"""The Scrollands' game state as a ledger: every change (keks, Chuckles, wounds, items, travel, quest clocks) is
an entry appended to the game_ledger table, and a character is the running total of their entries.

- **One way in.** Bot commands, right-click menus and the DM's tools all call `commit`, so the rules (no
  overdrafts, Chuckles cap at three, you can't hand over what you don't have) live here once.
- **Atomic.** A payment or a trade is a few entries under one transaction id: all of them land or none do.
- **Exact undo.** Entries are stored as they took effect (a heal at full HP is recorded as +0), so `undo` appends
  the precise inverse and nothing is ever deleted.
- **A memory.** The ledger is the campaign's history in order: `/character log`, the DM's session recap and the
  canon file all read from it.

The characters table is a cache of the fold (`replay` rebuilds it), and the world (quest flags and clocks) is
the fold of subject 0, cached in bot_state. Characters made before the ledger get a baseline entry of their
saved state the first time anything changes.
"""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass, field

from . import characters as rules
from . import world as places

WORLD = 0  # the ledger subject for world state
ITEM_KINDS = ("gear", "loot", "consumable", "quest", "curio")
MAX_ITEMS = 40


class LedgerError(ValueError):
    """A change the rules don't allow; nothing was written."""


@dataclass
class Entry:
    subject: int
    kind: str
    data: dict = field(default_factory=dict)
    note: str = ""
    id: int = 0
    actor: str = ""
    tx: str = ""
    undoes: int | None = None
    ts: float = 0.0

    @classmethod
    def from_row(cls, row: dict) -> Entry:
        return cls(row["subject"], row["kind"], row["data"], row["note"], row["id"], row["actor"], row["tx"], row["undoes"], row["ts"])


# ── Entries ─────────────────────────────────────────────────────────

def keks(user_id: int, amount: int, note: str = "") -> Entry:
    return Entry(user_id, "keks", {"n": int(amount)}, note)


def chuckles(user_id: int, amount: int = 1, note: str = "") -> Entry:
    return Entry(user_id, "chuckles", {"n": int(amount)}, note)


def hp(user_id: int, amount: int, note: str = "") -> Entry:
    """Negative is damage, positive heals."""
    return Entry(user_id, "hp", {"n": int(amount)}, note)


def level(user_id: int, amount: int = 1, note: str = "") -> Entry:
    return Entry(user_id, "level", {"n": int(amount)}, note)


def move(user_id: int, place: str, note: str = "") -> Entry:
    return Entry(user_id, "move", {"to": place}, note)


def gain(user_id: int, item: str, qty: int = 1, kind: str = "gear", note: str = "") -> Entry:
    return Entry(user_id, "gain", {"item": item.strip(), "qty": int(qty), "kind": kind}, note)


def lose(user_id: int, item: str, qty: int = 1, note: str = "") -> Entry:
    return Entry(user_id, "lose", {"item": item.strip(), "qty": int(qty)}, note)


def remember(user_id: int, text: str) -> Entry:
    return Entry(user_id, "note", {"text": text.strip()})


def baseline(user_id: int, data: dict, note: str = "") -> Entry:
    """A character's whole state: their creation, or where the ledger picks up an older character."""
    return Entry(user_id, "baseline", {"state": data}, note)


def flag(key: str, value, note: str = "") -> Entry:
    """A world fact: flag("bridge_burned", True), flag("omnius_mood", "petty"). None clears it."""
    return Entry(WORLD, "flag", {"key": key, "value": value}, note)


def clock(key: str, ticks: int = 1, size: int | None = None, label: str | None = None, note: str = "") -> Entry:
    """A progress clock (Blades in the Dark style): something coming, filled one segment at a time."""
    return Entry(WORLD, "clock", {"key": key, "ticks": int(ticks), "size": size, "label": label}, note)


def session(title: str) -> Entry:
    """A marker where a session starts, so a recap knows what's new."""
    return Entry(WORLD, "session", {"title": title.strip()})


# ── Applying an entry ───────────────────────────────────────────────

def _find(inventory: list, name: str) -> dict | None:
    wanted = name.casefold()
    return next((item for item in inventory if item["name"].casefold() == wanted), None)


def apply(state, entry: Entry):
    """Apply one entry to a Character (or the world dict, for subject 0) in place. Returns the entry's data as it
    actually took effect, which is what gets stored."""
    data, kind = dict(entry.data), entry.kind
    if kind == "baseline":
        data["was"] = None if state is None else (state if isinstance(state, dict) else state.to_dict())
        return data
    if entry.subject == WORLD:
        return _apply_world(state, kind, data)
    c: rules.Character = state
    if kind == "keks":
        if c.keks + data["n"] < 0:
            raise LedgerError(f"{c.name} has {c.keks} keks.")
        c.keks += data["n"]
    elif kind == "chuckles":
        data["n"] = max(0, min(rules.MAX_CHUCKLES, c.chuckles + data["n"])) - c.chuckles
        c.chuckles += data["n"]
    elif kind == "hp":
        data["n"] = max(0, min(c.max_hp, c.hp + data["n"])) - c.hp
        c.hp += data["n"]
    elif kind == "level":
        if c.level + data["n"] < 1:
            raise LedgerError("Level 1 is the floor.")
        c.level += data["n"]
    elif kind == "move":
        if data["to"] not in places.PLACES:
            raise LedgerError(f"Nowhere called {data['to']}.")
        data["from"] = data.get("from") or c.location
        c.location = data["to"]
    elif kind == "gain":
        name, qty = data["item"][:60], data["qty"]
        if not name or qty < 1:
            raise LedgerError("Name the item, and give at least one.")
        if data.get("kind") not in ITEM_KINDS:
            data["kind"] = "gear"
        held = _find(c.inventory, name)
        if held is None:
            if len(c.inventory) >= MAX_ITEMS:
                raise LedgerError(f"{c.name}'s pack is full ({MAX_ITEMS} kinds of thing).")
            held = {"name": name, "qty": 0, "kind": data["kind"], "note": entry.note[:80]}
            c.inventory.append(held)
        held["qty"] += qty
        data["item"] = held["name"]
    elif kind == "lose":
        held = _find(c.inventory, data["item"])
        if held is None or held["qty"] < data["qty"] or data["qty"] < 1:
            have = held["qty"] if held else 0
            raise LedgerError(f"{c.name} has {have} × {data['item']}.")
        held["qty"] -= data["qty"]
        data.update(item=held["name"], kind=held["kind"], was_note=held.get("note", ""))
        if not held["qty"]:
            c.inventory.remove(held)
    elif kind == "note":
        log = c.notes.setdefault("log", [])
        if data.get("remove"):
            if data["text"] in log:
                log.remove(data["text"])
        else:
            log.append(data["text"][:200])
    else:
        raise LedgerError(f"Unknown entry kind {kind}.")
    return data


def _apply_world(state: dict, kind: str, data: dict) -> dict:
    if kind == "flag":
        flags = state.setdefault("flags", {})
        data["was"] = flags.get(data["key"])
        if data["value"] is None:
            flags.pop(data["key"], None)
        else:
            flags[data["key"]] = data["value"]
    elif kind == "clock":
        clocks = state.setdefault("clocks", {})
        current = clocks.get(data["key"])
        data["was"] = copy.deepcopy(current)
        if "set" in data:  # an undo puts the clock back exactly as it was
            if data["set"] is None:
                clocks.pop(data["key"], None)
            else:
                clocks[data["key"]] = copy.deepcopy(data["set"])
            return data
        if current is None:
            if not data.get("size"):
                raise LedgerError(f"New clock {data['key']}: give it a size (4, 6 or 8 segments).")
            current = clocks[data["key"]] = {"label": data.get("label") or data["key"], "size": int(data["size"]), "filled": 0}
        if data.get("size"):
            current["size"] = int(data["size"])
        if data.get("label"):
            current["label"] = data["label"]
        current["filled"] = max(0, min(current["size"], current["filled"] + data["ticks"]))
    elif kind == "session":
        state["session"] = data["title"]
    else:
        raise LedgerError(f"Unknown world entry {kind}.")
    return data


def inverse(entry: Entry) -> Entry:
    """The entry that exactly cancels one already applied."""
    d, kind = entry.data, entry.kind
    if kind in ("keks", "chuckles", "hp", "level"):
        return Entry(entry.subject, kind, {"n": -d["n"]})
    if kind == "move":
        return Entry(entry.subject, "move", {"to": d["from"], "from": d["to"]})
    if kind == "gain":
        return Entry(entry.subject, "lose", {"item": d["item"], "qty": d["qty"]})
    if kind == "lose":
        return Entry(entry.subject, "gain", {"item": d["item"], "qty": d["qty"], "kind": d.get("kind", "gear")}, d.get("was_note", ""))
    if kind == "note":
        return Entry(entry.subject, "note", {"text": d["text"], "remove": not d.get("remove")})
    if kind == "flag":
        return Entry(WORLD, "flag", {"key": d["key"], "value": d["was"]})
    if kind == "clock":
        return Entry(WORLD, "clock", {"key": d["key"], "ticks": 0, "set": d["was"]})
    raise LedgerError(f"A {kind} entry can't be undone.")


# ── Reading and writing ─────────────────────────────────────────────

def world_state(store, guild_id: int | None) -> dict:
    raw = store.state(f"world:{guild_id or 0}")
    return json.loads(raw) if raw else {"flags": {}, "clocks": {}}


def commit(store, guild_id: int | None, entries: list[Entry], actor: str = "", undoes: int | None = None) -> list[Entry]:
    """Apply entries in order and write them, all or nothing. Raises LedgerError (writing nothing) if any breaks
    a rule. Returns the entries as stored."""
    tx = uuid.uuid4().hex[:12]
    stored: list[Entry] = []
    with store.game_transaction():
        states: dict[int, object] = {}
        for entry in entries:
            subject = entry.subject
            if subject not in states:
                if subject == WORLD:
                    states[subject] = world_state(store, guild_id)
                else:
                    data = store.character(guild_id, subject)
                    if data is not None and not store.ledger(guild_id, subject, limit=1):
                        store.add_ledger(guild_id, subject, "baseline", {"state": data, "was": None}, "where the ledger begins", "ledger", tx)
                    states[subject] = rules.Character.from_dict(data) if data else None
            state = states[subject]
            if entry.kind == "baseline":
                applied = apply(state, entry)
                states[subject] = rules.Character.from_dict(entry.data["state"])
            elif state is None:
                raise LedgerError("No character for that member yet.")
            else:
                applied = apply(state, entry)
            entry_id = store.add_ledger(guild_id, subject, entry.kind, applied, entry.note, actor, tx, undoes)
            stored.append(Entry(subject, entry.kind, applied, entry.note, entry_id, actor, tx, undoes))
        for subject, state in states.items():
            if subject == WORLD:
                store.set_state(f"world:{guild_id or 0}", json.dumps(state, ensure_ascii=False))
            elif state is not None:
                store.save_character(guild_id, subject, state.to_dict())
    return stored


def undo(store, guild_id: int | None, entry_id: int, actor: str = "") -> list[Entry]:
    """Cancel an entry and everything committed with it (both halves of a payment), by appending inverses."""
    row = store.ledger_entry(guild_id, entry_id)
    if row is None:
        raise LedgerError(f"No ledger entry {entry_id}.")
    if row["undoes"] is not None:
        raise LedgerError(f"Entry {entry_id} is itself an undo; undo isn't undoable.")
    group = [Entry.from_row(r) for r in store.ledger(guild_id, tx=row["tx"]) if r["kind"] != "baseline" or r["id"] == entry_id]
    if any(store.ledger_undone(guild_id, e.id) for e in group):
        raise LedgerError(f"Entry {entry_id} was already undone.")
    return commit(store, guild_id, [inverse(e) for e in reversed(group)], actor, undoes=entry_id)


def replay(store, guild_id: int | None, subject: int):
    """Rebuild a character (or the world) from the ledger alone: the check that the cache is honest."""
    state = {"flags": {}, "clocks": {}} if subject == WORLD else None
    for row in store.ledger(guild_id, subject):
        entry = Entry.from_row(row)
        if entry.kind == "baseline":
            state = rules.Character.from_dict(entry.data["state"])
            continue
        apply(state, Entry(subject, entry.kind, dict(entry.data), entry.note))
    return state


# ── In words ────────────────────────────────────────────────────────

def clock_bar(clock: dict) -> str:
    return "◆" * clock["filled"] + "◇" * (clock["size"] - clock["filled"])


def describe(entry: Entry, name: str = "") -> str:
    """One entry as a line of the chronicle: "Stackhouse +50 keks · kept the oath"."""
    d, kind, who = entry.data, entry.kind, name or "Someone"
    signed = rules.signed
    if kind == "baseline":
        text = f"{who} enters the ledger" if d.get("was") else f"{who} enters the Scrollands"
    elif kind == "keks":
        text = f"{who} {signed(d['n'])} keks"
    elif kind == "chuckles":
        text = f"{who} {signed(d['n'])} Chuckle{'s' if abs(d['n']) != 1 else ''}"
    elif kind == "hp":
        text = f"{who} takes {-d['n']} damage" if d["n"] < 0 else f"{who} heals {d['n']}"
    elif kind == "level":
        text = f"{who} {signed(d['n'])} level"
    elif kind == "move":
        text = f"{who} travels to {places.place(d['to']).name}"
    elif kind == "gain":
        text = f"{who} gains {d['item']}" + (f" ×{d['qty']}" if d["qty"] != 1 else "")
    elif kind == "lose":
        text = f"{who} loses {d['item']}" + (f" ×{d['qty']}" if d["qty"] != 1 else "")
    elif kind == "note":
        text = f"{who}: {'struck: ' if d.get('remove') else ''}{d['text']}"
    elif kind == "flag":
        text = f"The world: {d['key']} cleared" if d["value"] is None else f"The world: {d['key']} = {d['value']}"
    elif kind == "clock":
        text = f"Clock {d['key']} restored" if "set" in d else f"Clock {d.get('label') or d['key']} {signed(d['ticks'])}"
    elif kind == "session":
        text = f"Session: {d['title']}"
    else:
        text = f"{who}: {kind}"
    if entry.undoes:
        text = f"undo #{entry.undoes}: {text}"
    return text + (f" · {entry.note}" if entry.note and kind != "baseline" else "")
