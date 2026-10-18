"""The DM's game-state tools. Every change goes through the ledger (psychograph/ledger.py): checked, atomic,
undoable, and remembered in order. A target is a user id, part of a character's name, or `all`.

    python tools/dm/game.py party                       # everyone: class, scores, HP, keks, Chuckles, place, pack, notes
    python tools/dm/game.py world                       # quest clocks, flags, the current session
    python tools/dm/game.py roll <who> perception [adv|dis] [reason...]   # roll for a player, posted as the bot

    python tools/dm/game.py attack <who> --ac 13 [adv|dis] [--bonus 1d6] [reason...]   # a player attacks a monster
    python tools/dm/game.py strike "Lurker Spawn" <who> --atk 4 --dmg 1d8+2 [adv|dis] [reason...]   # a monster attacks
        Both post the roll as the bot. A strike that lands takes the damage off the player's HP through the ledger.

    python tools/dm/game.py reward <who|all> --keks 50 --chuckles 1 --note "kept the oath"
    python tools/dm/game.py hurt <who> 3 [why...]       # damage; a negative number heals
    python tools/dm/game.py give <who> "Rope of Hanging" [--qty 2] [--kind gear|loot|consumable|quest|curio] [--note "from the Dead Chat"]
    python tools/dm/game.py take <who> "Rope of Hanging" [--qty 1] [--note "burned in the bog"]
    python tools/dm/game.py move <who|all> deadchat
    python tools/dm/game.py level <who|all> [--note "survived the Dead Chat"]
    python tools/dm/game.py note <who> "owes Omnius one favour"

    python tools/dm/game.py clock lurker +1 [--size 6 --label "The Lurker King stirs"]   # a new clock needs --size
    python tools/dm/game.py flag bridge_burned yes      # `clear` removes a flag
    python tools/dm/game.py session "II: The Dead Chat" # mark a session start, for the recap

    python tools/dm/game.py log [who] [--n 20]          # the ledger, newest last, with entry ids
    python tools/dm/game.py recap                       # everything since the last session mark, for the canon file
    python tools/dm/game.py undo <entry id>             # cancel an entry (and the rest of its transaction)

Everything writes to the SQLite store the bot reads, so the next /character sheet shows it.
"""

from __future__ import annotations

import io
import json
import sys
import uuid

from common import call, config

from psychograph import characters as rules
from psychograph import combat, icons, ledger, world
from psychograph.settings import load_settings
from psychograph.store import Store

cfg = config()
GUILD = int(cfg["guild_id"])
store = Store(load_settings().db_path)
FLAGS = ("--keks", "--chuckles", "--note", "--qty", "--kind", "--size", "--label", "--n", "--ac", "--bonus", "--atk", "--dmg")


def flag(name: str, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def words(start: int) -> list[str]:
    """Positional words from argv[start:], without the --flags and their values."""
    out, skip = [], False
    for word in sys.argv[start:]:
        if skip:
            skip = False
        elif word in FLAGS:
            skip = True
        else:
            out.append(word)
    return out


def names() -> dict[int, str]:
    return {uid: data["name"] for uid, data in store.characters(GUILD)}


def targets(who: str) -> list[int]:
    roster = names()
    if who == "all":
        return list(roster)
    if who.isdigit() and int(who) in roster:
        return [int(who)]
    found = [uid for uid, name in roster.items() if who.casefold() in name.casefold()]
    if len(found) != 1:
        raise SystemExit(f"{'No' if not found else 'More than one'} character matches {who!r}. Known: "
                         + ", ".join(f"{name} ({uid})" for uid, name in roster.items()))
    return found


def commit(entries: list[ledger.Entry]) -> None:
    try:
        stored = ledger.commit(store, GUILD, entries, "dm")
    except ledger.LedgerError as error:
        raise SystemExit(f"Refused, nothing changed: {error}")
    roster = names()
    for entry in stored:
        print(f"#{entry.id} {ledger.describe(entry, roster.get(entry.subject, ''))}")


def party() -> None:
    for user_id, data in store.characters(GUILD):
        c = rules.Character.from_dict(data)
        scores = " ".join(f"{rules.STAT_NAMES[s]} {c.scores[s]}({rules.signed(c.mod(s))})" for s in rules.STATS)
        pack = ", ".join(f"{i['name']}{' ×' + str(i['qty']) if i['qty'] != 1 else ''}{' [quest]' if i['kind'] == 'quest' else ''}"
                         for i in c.inventory) or "empty"
        print(f"{user_id} · {c.name} · L{c.level} {c.role_info.name} ({c.role_info.classic}) · {c.faction_info.name} · {scores} · "
              f"HP {c.hp}/{c.max_hp} · {c.keks} keks · {c.chuckles} Chuckles · at {world.place(c.location).name} · pack: {pack}"
              f"{' · look: ' + c.look if c.look else ''} · keepsake: {c.keepsake} · notes: {json.dumps(c.notes.get('log', []))}")


def world_status() -> None:
    state = ledger.world_state(store, GUILD)
    print(f"session: {state.get('session', '(none marked)')}")
    for key, clock in state.get("clocks", {}).items():
        print(f"clock {key}: {ledger.clock_bar(clock)} {clock['filled']}/{clock['size']} {clock['label']}")
    for key, value in state.get("flags", {}).items():
        print(f"flag {key} = {value}")


def show(rows: list[dict]) -> None:
    roster = names()
    for row in rows:
        entry = ledger.Entry.from_row(row)
        print(f"#{entry.id} [{entry.actor or '?'}] {ledger.describe(entry, roster.get(entry.subject, ''))}")


def recap() -> None:
    rows = store.ledger(GUILD)
    marks = [row["id"] for row in rows if row["kind"] == "session"]
    start = marks[-1] if marks else 0
    show([row for row in rows if row["id"] >= start and row["actor"] != "ledger"])  # skip the automatic baselines
    world_status()


def post(text: str, dice_list: list[tuple[int, str, str]]) -> None:
    """Post a roll as the bot, with the drawn dice."""
    picture = io.BytesIO()
    icons.roll_image(dice_list).save(picture, format="PNG")
    boundary = uuid.uuid4().hex
    payload = json.dumps({"content": text, "allowed_mentions": {"parse": []}, "attachments": [{"id": 0, "filename": "roll.png"}]})
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\nContent-Type: application/json\r\n\r\n{payload}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"roll.png\"\r\nContent-Type: image/png\r\n\r\n"
            ).encode() + picture.getvalue() + f"\r\n--{boundary}--\r\n".encode()
    call("POST", f"/channels/{cfg['channel_id']}/messages", raw=body, content_type=f"multipart/form-data; boundary={boundary}")


def _attack_dice(result: combat.Attack) -> list[tuple[int, str, str]]:
    natural = "crit" if result.kept == 20 else "fumble" if result.kept == 1 else "normal"
    dice_list = [(20, str(result.total), natural)]
    if result.damage:
        dice_list.append((combat.die_sides(result.damage_dice), str(result.damage), "crit" if result.crit else "normal"))
    return dice_list


def lean_of(args: list[str]) -> str:
    return next((a[:3].lower() for a in args if a.lower() in ("adv", "dis", "advantage", "disadvantage")), "")


def attack(user_id: int, args: list[str]) -> None:
    """A player's character attacks a monster: their class weapon against --ac, plus any --bonus dice."""
    character = rules.Character.from_dict(store.character(GUILD, user_id))
    reason = " ".join(a for a in args if a.lower() not in ("adv", "dis", "advantage", "disadvantage"))
    ac = flag("--ac")
    result = combat.character_strike(character, "", int(ac) if ac else None, lean_of(args), flag("--bonus", ""), reason)
    post(combat.render(result, "rolled by the Narrator"), _attack_dice(result))
    print(f"{result.total} to hit, {'hit' if result.hit else 'miss' if result.hit is False else '?'}, {result.damage} damage")


def strike(monster: str, user_id: int, args: list[str]) -> None:
    """A monster attacks a character: --atk bonus and --dmg notation, against the character's defence."""
    character = rules.Character.from_dict(store.character(GUILD, user_id))
    reason = " ".join(a for a in args if a.lower() not in ("adv", "dis", "advantage", "disadvantage"))
    try:
        result = combat.strike(monster, character.name, int(flag("--atk", 0)), flag("--dmg", "1d6"), combat.defence(character),
                               lean_of(args), reason=reason)
    except combat.CombatError as error:
        raise SystemExit(str(error))
    text = combat.render(result, "the Narrator")
    if result.hit:
        ledger.commit(store, GUILD, [ledger.hp(user_id, -result.damage, f"{monster}{', ' + reason if reason else ''}")], "dm")
        character = rules.Character.from_dict(store.character(GUILD, user_id))
        text += f"\n-# {character.name} · HP {character.hp}/{character.max_hp}" + (" · **down**" if character.hp <= 0 else "")
    post(text, _attack_dice(result))
    print(f"{result.total} vs {result.defence}: {'hit for ' + str(result.damage) if result.hit else 'miss'} · {character.name} HP {character.hp}/{character.max_hp}")


def roll(user_id: int, request_words: list[str]) -> None:
    request = rules.stat_request(" ".join(request_words))
    if request is None:
        raise SystemExit("Name a score or skill: mog, perception, save cope …")
    character = rules.Character.from_dict(store.character(GUILD, user_id))
    result = rules.check(character, *request)
    tone = {"crit": "crit", "fumble": "fumble"}.get(result.band[0], "normal")
    post(rules.render_check(result, character, "rolled by the Narrator"), [(20, str(result.total), tone)])
    print(result.total, result.band[1])


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "party"
    args = words(2)
    note = flag("--note", "")
    if command == "party":
        party()
    elif command == "world":
        world_status()
    elif command == "recap":
        recap()
    elif command == "log":
        subject = targets(args[0])[0] if args else None
        show(store.ledger(GUILD, subject, limit=int(flag("--n", 20))))
    elif command == "undo":
        try:
            stored = ledger.undo(store, GUILD, int(args[0]), "dm")
        except ledger.LedgerError as error:
            raise SystemExit(f"Refused, nothing changed: {error}")
        show([store.ledger_entry(GUILD, entry.id) for entry in stored])
    elif command == "clock":
        commit([ledger.clock(args[0], int(args[1]) if len(args) > 1 else 1, int(flag("--size", 0)) or None, flag("--label"), note)])
    elif command == "flag":
        value = None if args[1].lower() == "clear" else " ".join(args[1:])
        commit([ledger.flag(args[0], value, note)])
    elif command == "session":
        commit([ledger.session(" ".join(args))])
    elif command == "roll":
        roll(targets(args[0])[0], args[1:])
    elif command == "attack":
        attack(targets(args[0])[0], args[1:])
    elif command == "strike":
        strike(args[0], targets(args[1])[0], args[2:])
    else:
        who = targets(args[0])
        if command == "reward":
            entries = [e for uid in who for e in (
                [ledger.keks(uid, int(flag("--keks", 0)), note)] if flag("--keks") else []) + (
                [ledger.chuckles(uid, int(flag("--chuckles", 0)), note)] if flag("--chuckles") else [])]
        elif command == "hurt":
            entries = [ledger.hp(uid, -int(args[1]), note or " ".join(args[2:])) for uid in who]
        elif command == "give":
            entries = [ledger.gain(uid, args[1], int(flag("--qty", 1)), flag("--kind", "gear"), note) for uid in who]
        elif command == "take":
            entries = [ledger.lose(uid, args[1], int(flag("--qty", 1)), note) for uid in who]
        elif command == "move":
            place = world.find_place(args[1]) or world.PLACES.get(args[1])
            if place is None:
                raise SystemExit(f"Unknown place. Known: {', '.join(world.PLACES)}")
            entries = [ledger.move(uid, place.key, note) for uid in who]
        elif command == "level":
            entries = [ledger.level(uid, 1, note) for uid in who]
        elif command == "note":
            entries = [ledger.remember(uid, " ".join(args[1:])) for uid in who]
        else:
            raise SystemExit(__doc__)
        if not entries:
            raise SystemExit("Nothing to change.")
        commit(entries)


if __name__ == "__main__":
    main()
