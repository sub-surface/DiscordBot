"""The DM's game-state tools: read the party, roll for a player, hand out rewards, move characters.

    python tools/dm/game.py party                          # every character: class, scores, keks, Chuckles, place
    python tools/dm/game.py roll <user_id> perception [adv|dis] [reason...]   # roll for a player, posted as the bot
    python tools/dm/game.py reward <user_id> --keks 50 --chuckles 1 --note "kept the oath"
    python tools/dm/game.py hurt <user_id> 3               # damage (negative heals)
    python tools/dm/game.py move <user_id|all> deadchat    # move a character (or everyone) on the map
    python tools/dm/game.py note <user_id> "owes Omnius one favour"

Everything writes to the same SQLite store the bot reads, so the next /character sheet shows it.
"""

from __future__ import annotations

import io
import json
import sys
import uuid

from common import call, config

from psychograph import characters as rules
from psychograph import icons, world
from psychograph.settings import load_settings
from psychograph.store import Store

cfg = config()
GUILD = int(cfg["guild_id"])
store = Store(load_settings().db_path)


def load(user_id: int) -> rules.Character:
    data = store.character(GUILD, user_id)
    if data is None:
        raise SystemExit(f"No character for {user_id}.")
    return rules.Character.from_dict(data)


def save(user_id: int, character: rules.Character) -> None:
    store.save_character(GUILD, user_id, character.to_dict())


def flag(name: str, default=None):
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv else default


def party() -> None:
    for user_id, data in store.characters(GUILD):
        c = rules.Character.from_dict(data)
        scores = " ".join(f"{rules.STAT_NAMES[s]} {c.scores[s]}({rules.signed(c.mod(s))})" for s in rules.STATS)
        print(f"{user_id} · {c.name} · {c.role_info.name} ({c.role_info.classic}) · {c.faction_info.name} · {scores} · "
              f"HP {c.hp}/{c.max_hp} · {c.keks} keks · {c.chuckles} Chuckles · at {world.place(c.location).name}"
              f"{' · look: ' + c.look if c.look else ''} · keepsake: {c.keepsake} · notes: {json.dumps(c.notes)}")


def roll(user_id: int, words: list[str]) -> None:
    request = rules.stat_request(" ".join(words))
    if request is None:
        raise SystemExit("Name a score or skill: mog, perception, save cope …")
    character = load(user_id)
    result = rules.check(character, *request)
    tone = {"crit": "crit", "fumble": "fumble"}.get(result.band[0], "normal")
    text = rules.render_check(result, character, "rolled by the Narrator")
    picture = io.BytesIO()
    icons.roll_image([(20, str(result.total), tone)]).save(picture, format="PNG")
    boundary = uuid.uuid4().hex
    payload = json.dumps({"content": text, "allowed_mentions": {"parse": []}, "attachments": [{"id": 0, "filename": "roll.png"}]})
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\nContent-Type: application/json\r\n\r\n{payload}\r\n"
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"roll.png\"\r\nContent-Type: image/png\r\n\r\n"
            ).encode() + picture.getvalue() + f"\r\n--{boundary}--\r\n".encode()
    call("POST", f"/channels/{cfg['channel_id']}/messages", raw=body, content_type=f"multipart/form-data; boundary={boundary}")
    print(result.total, result.band[1])


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "party"
    if command == "party":
        party()
        return
    target = sys.argv[2]
    if command == "roll":
        roll(int(target), [w for w in sys.argv[3:]])
    elif command == "move":
        place = world.find_place(sys.argv[3]) or world.PLACES.get(sys.argv[3])
        if place is None:
            raise SystemExit(f"Unknown place. Known: {', '.join(world.PLACES)}")
        ids = [uid for uid, _ in store.characters(GUILD)] if target == "all" else [int(target)]
        for uid in ids:
            c = load(uid)
            c.location = place.key
            save(uid, c)
        print(f"moved {len(ids)} to {place.name}")
    else:
        c = load(int(target))
        if command == "reward":
            c.keks += int(flag("--keks", 0))
            c.chuckles = min(rules.MAX_CHUCKLES, c.chuckles + int(flag("--chuckles", 0)))
            if flag("--note"):
                c.notes.setdefault("log", []).append(flag("--note"))
        elif command == "hurt":
            c.hp = max(0, min(c.max_hp, c.hp - int(sys.argv[3])))
        elif command == "note":
            c.notes.setdefault("log", []).append(" ".join(sys.argv[3:]))
        else:
            raise SystemExit(__doc__)
        save(int(target), c)
        print(f"{c.name}: HP {c.hp}/{c.max_hp}, {c.keks} keks, {c.chuckles} Chuckles")


if __name__ == "__main__":
    main()
