"""Post as the Narrator or an NPC through the game channel's webhook.

    python tools/dm/say.py narrator scene.txt --ping 1323...,9103...
    python tools/dm/say.py omnius line.txt
    python tools/dm/say.py --as "A Nervous Merchant" narrator line.txt   # a one-off name, an existing avatar

The text comes from a file, so quotes, block quotes and newlines survive the shell. Mentions are only allowed
for the user IDs passed with --ping.
"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

from common import config

args = sys.argv[1:]
pings = args[args.index("--ping") + 1].split(",") if "--ping" in args else []
custom = args[args.index("--as") + 1] if "--as" in args else None
positional = [a for i, a in enumerate(args) if not a.startswith("--") and (i == 0 or args[i - 1] not in ("--ping", "--as"))]
voice, text_file = positional[0], positional[1]
cfg = config()
name = custom or cfg["npcs"][voice][0]
body = {"content": Path(text_file).read_text(encoding="utf-8").strip()[:2000], "username": name,
        "avatar_url": cfg["avatars"][voice], "allowed_mentions": {"parse": [], "users": pings}}
request = urllib.request.Request(cfg["webhook"] + "?wait=true", data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "DiscordBot (psychograph-dm, 1.0)"})
with urllib.request.urlopen(request, timeout=30) as response:
    print(json.load(response)["id"])
