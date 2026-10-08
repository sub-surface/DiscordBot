"""Keep chatter personas grounded in the server's real messages.

  crawl   fetch the server's text channels into data/logs/<channel>.jsonl (only what's new since the last run)
  slice   one file per chatter for a model to read: their own lines, then lines that mention them
  tics    each chatter's recurring phrases and stock replies, ranked by how much more they say them than
          everyone else: the raw material for `says`, which should be phrases they keep using
  check   how often each `says` line turns up in that member's messages (0 = never; --also adds archives)
  lore    the most-reacted messages, as candidates for lore.json

Chatters are matched to members by the `handle` (Discord username) in their persona file. Reading
history needs a user token: DISCORD_USER_TOKEN, else the DISCORD_TOKEN in --env (by default the
chronicle's). The logs stay local (data/ is gitignored).

    python tools/persona_logs.py crawl
    python tools/persona_logs.py slice --out slices
    python tools/persona_logs.py tics
    python tools/persona_logs.py check --also ../Archive/philchat-chronicle/chronicler/data/archive.db
    python tools/persona_logs.py lore --top 40
"""

from __future__ import annotations

import argparse
import math
import json
import os
import random
import re
import sqlite3
import sys
import time
from collections import Counter
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOGS = ROOT / "data" / "logs"
CHATTERS = ROOT / "personas" / "chatters"
GUILD = "1433428321735147641"  # Liberated Speech
CHRONICLE_ENV = ROOT.parent / "Archive" / "philchat-chronicle" / "chronicler" / ".env"
API = "https://discord.com/api/v10"


def token(env: Path) -> str:
    if os.environ.get("DISCORD_USER_TOKEN"):
        return os.environ["DISCORD_USER_TOKEN"]
    match = re.search(r"^DISCORD_TOKEN=(.+)$", env.read_text(encoding="utf-8"), re.M)
    if not match:
        sys.exit(f"No DISCORD_USER_TOKEN set and no DISCORD_TOKEN in {env}")
    return match.group(1).strip()


def get(path: str, auth: str):
    while True:
        request = urllib.request.Request(API + path, headers={"Authorization": auth, "User-Agent": "persona-logs"})
        try:
            with urllib.request.urlopen(request) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code == 429:
                time.sleep(json.load(error).get("retry_after", 2) + 0.5)
                continue
            if error.code in (403, 404):
                return None
            raise


def jsonl(file: Path) -> list[str]:
    return [line for line in file.read_text(encoding="utf-8").split("\n") if line]  # not splitlines: texts hold U+2028


def crawl(args) -> None:
    auth = token(args.env)
    LOGS.mkdir(parents=True, exist_ok=True)
    for channel in get(f"/guilds/{args.guild}/channels", auth) or []:
        if channel["type"] != 0:
            continue
        file = LOGS / f"{channel['name']}.jsonl"
        rows = [json.loads(line) for line in jsonl(file)] if file.exists() else []
        after, new = (rows[-1]["id"] if rows else "0"), []
        while batch := get(f"/channels/{channel['id']}/messages?limit=100&after={after}", auth):
            batch.sort(key=lambda m: int(m["id"]))
            new += [
                {
                    "id": m["id"],
                    "ts": m["timestamp"][:16],
                    "user": m["author"]["username"],
                    "uid": m["author"]["id"],
                    "name": (m.get("member") or {}).get("nick") or m["author"].get("global_name") or m["author"]["username"],
                    "bot": bool(m["author"].get("bot") or m.get("webhook_id")),
                    "text": m.get("content") or "",
                    "files": len(m.get("attachments") or []),
                    "reacts": sum(r.get("count", 0) for r in m.get("reactions") or []),
                }
                for m in batch
            ]
            after = batch[-1]["id"]
            time.sleep(0.25)
        with file.open("a", encoding="utf-8") as out:
            out.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in new)
        print(f"#{channel['name']}: +{len(new)} ({len(rows) + len(new)})")


def messages() -> list[tuple[str, dict]]:
    if not LOGS.exists():
        sys.exit("No logs yet: run `crawl` first")
    rows = [(f.stem, json.loads(line)) for f in LOGS.glob("*.jsonl") for line in jsonl(f)]
    return sorted((row for row in rows if not row[1]["bot"] and row[1]["text"].strip()), key=lambda row: int(row[1]["id"]))


def corpus(also: list[Path]) -> list[tuple[str, str]]:
    """(username, text) for every member message: the crawled logs plus any chronicle archives."""
    rows = [(m["user"], m["text"]) for _, m in messages()]
    for archive in also:
        with sqlite3.connect(archive) as db:
            rows += db.execute("select author_username, content from messages where content != ''").fetchall()
    return rows


def chatters() -> dict[str, dict]:
    return {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in sorted(CHATTERS.glob("*.json"))}


def line(channel: str, m: dict) -> str:
    return f"#{channel} {m['ts'][:10]} {m['name']}({m['user']}): {m['text'][:300].replace(chr(10), ' / ')}"


def sample(lines: list[str], budget: int, rng: random.Random) -> list[str]:
    """Lines in order, evenly thinned until they fit in `budget` characters."""
    while lines and sum(len(x) + 1 for x in lines) > budget:
        keep = sorted(rng.sample(range(len(lines)), int(len(lines) * 0.85)))
        lines = [lines[i] for i in keep]
    return lines


def slice_(args) -> None:
    rng, every = random.Random(1), messages()
    args.out.mkdir(parents=True, exist_ok=True)
    for key, data in chatters().items():
        handle = data.get("handle")
        if not handle:
            continue
        alias = re.compile(rf"\b({re.escape(key)}|{re.escape(handle)})\b", re.I)
        own = [line(c, m) for c, m in every if m["user"] == handle]
        about = [line(c, m) for c, m in every if m["user"] != handle and alias.search(m["text"])]
        body = ["## THEIR MESSAGES", *sample(own, args.chars * 3 // 4, rng), "", "## OTHERS MENTIONING THEM", *sample(about, args.chars // 4, rng)]
        (args.out / f"{key}.txt").write_text("\n".join(body), encoding="utf-8")
        print(f"{key}: {len(own)} own, {len(about)} about")


WORD = re.compile(r"<a?:\w+:\d+>|[\w']+")


def grams(text: str) -> set[str]:
    """The 2–5 word phrases in a message (custom emotes count as words), once each."""
    words = [w.casefold() for w in WORD.findall(text)]
    return {" ".join(words[i : i + n]) for n in range(2, 6) for i in range(len(words) - n + 1)}


def tics(args) -> None:
    """Phrases each chatter uses at least --min times, scored by log-odds against the rest of the server."""
    everyone, users = Counter(), {}
    for user, text in corpus(args.also):
        found = grams(text) | ({"= " + norm(text)} if len(text) <= 40 else set())  # "= …": a whole stock message
        everyone.update(found)
        users.setdefault(user, Counter()).update(found)
    total = sum(everyone.values())
    args.out.mkdir(parents=True, exist_ok=True)
    for key, data in chatters().items():
        mine = users.get(data.get("handle", ""))
        if not mine:
            continue
        size = sum(mine.values())

        def score(g: str) -> float:
            rest = everyone[g] - mine[g]
            return math.log((mine[g] + 0.5) / size) - math.log((rest + 0.5) / (total - size + 1)) + 0.3 * math.log(mine[g])

        kept: list[str] = []
        for g in sorted((g for g, n in mine.items() if n >= args.min), key=score, reverse=True):
            if not any(g.lstrip("= ") in k for k in kept):  # skip a phrase that only repeats a longer, better one
                kept.append(g)
            if len(kept) == args.top:
                break
        lines = [f"{mine[g]:>4}x (others {everyone[g] - mine[g]}) {g}" for g in kept]
        (args.out / f"{key}.txt").write_text("\n".join(lines), encoding="utf-8")
        print(f"{key}: {len(lines)} tics")


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(str.maketrans("’‘“”", "''\"\""))).strip().casefold()


def check(args) -> None:
    by_user: dict[str, list[str]] = {}
    for user, text in corpus(args.also):
        by_user.setdefault(user, []).append(norm(text))
    for key, data in chatters().items():
        written = by_user.get(data.get("handle", ""), [])
        for said in data.get("says", []):
            print(f"{sum(norm(said) in text for text in written):>4}x {key}: {said}")


def lore(args) -> None:
    top = sorted(messages(), key=lambda row: row[1]["reacts"], reverse=True)[: args.top]
    for channel, m in top:
        print(f"{m['reacts']:>3} {line(channel, m)}{' [file]' if m['files'] else ''}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--env", type=Path, default=CHRONICLE_ENV)
    parser.add_argument("--guild", default=GUILD)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("crawl").set_defaults(run=crawl)
    p = sub.add_parser("slice")
    p.add_argument("--out", type=Path, default=ROOT / "data" / "slices")
    p.add_argument("--chars", type=int, default=110_000, help="size of each slice (~4 chars a token)")
    p.set_defaults(run=slice_)
    p = sub.add_parser("tics")
    p.add_argument("--out", type=Path, default=ROOT / "data" / "tics")
    p.add_argument("--min", type=int, default=3, help="times they must have used a phrase")
    p.add_argument("--top", type=int, default=120)
    p.add_argument("--also", type=Path, action="append", default=[], help="a chronicle archive.db to count too")
    p.set_defaults(run=tics)
    p = sub.add_parser("check")
    p.add_argument("--also", type=Path, action="append", default=[], help="a chronicle archive.db to count too")
    p.set_defaults(run=check)
    p = sub.add_parser("lore")
    p.add_argument("--top", type=int, default=40)
    p.set_defaults(run=lore)
    args = parser.parse_args()
    args.run(args)


if __name__ == "__main__":
    main()
