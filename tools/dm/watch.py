"""Print each new game-channel message as one line, for Claude's Monitor tool. Skips the Narrator's own posts.

    python -u tools/dm/watch.py            # from the newest message on
    python -u tools/dm/watch.py --since 1561128934664573002   # catch up from a message id first

Lines look like `Hugh Chungus <1323…>: roll lore` (members, with their id for @-mentions) or
`Mechirk Fucking Epstein [bot]: **18** · Yes. …` (the bot's rolls and sheets).
"""

from __future__ import annotations

import sys
import time

from common import call, config

cfg = config()
hook_id = cfg["webhook"].split("/webhooks/")[1].split("/")[0]
path = f"/channels/{cfg['channel_id']}/messages"
last = sys.argv[sys.argv.index("--since") + 1] if "--since" in sys.argv else call("GET", f"{path}?limit=1")[0]["id"]
while True:
    try:
        for m in sorted(call("GET", f"{path}?after={last}&limit=50"), key=lambda m: int(m["id"])):
            last = m["id"]
            if m.get("webhook_id") == hook_id:
                continue
            who = m["author"].get("global_name") or m["author"]["username"]
            tag = " [bot]" if m["author"].get("bot") else f" <{m['author']['id']}>"
            text = (m.get("content") or "").replace("\n", " ⏎ ")
            extra = [a["filename"] for a in m.get("attachments", [])]
            extra += [e.get("title") or (e.get("description") or "")[:80] for e in m.get("embeds", [])]
            if m.get("interaction_metadata"):
                extra.append("/command")
            print(f"{who}{tag}: {text}" + (f"  [{', '.join(x for x in extra if x)}]" if extra else ""), flush=True)
    except Exception as error:  # keep watching through network blips
        print(f"(watch error: {error})", flush=True)
    time.sleep(6)
