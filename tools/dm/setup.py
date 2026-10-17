"""One-off DM setup: find the game channel, create the Narrator webhook, upload NPC monogram avatars as
application emojis (their CDN links never expire), and save it all to data/dm.json.

    python tools/dm/setup.py            # idempotent: reuses what already exists
    python tools/dm/setup.py --npc "Omnius, the God Machine" O 96cdff   # add or redraw one NPC voice
    python tools/dm/setup.py --owner=132301139667451904   # whose `-#` whispers may steer the game
"""

from __future__ import annotations

import base64
import io
import json
import re
import sys

from common import CONFIG, ROOT, call, settings

from PIL import Image, ImageDraw, ImageFont

BRASS, BONE, CHARCOAL = (200, 161, 90), (236, 230, 218), (20, 22, 27)
DEFAULT_NPCS = {  # key → (display name, monogram letter, ring colour hex)
    "narrator": ("The Narrator", "N", "c8a15a"),
    "omnius": ("Omnius, the God Machine", "O", "96cdff"),
    "landlord": ("The Landlord", "L", "b4453a"),
    "bernard": ("Bernard, keeper of records", "B", "ece6da"),
    "minime": ("Mini Me", "M", "96e8aa"),
    "crier": ("The Crier", "C", "c8a15a"),
}


def monogram(letter: str, ring_hex: str) -> bytes:
    ring = tuple(int(ring_hex[i:i + 2], 16) for i in (0, 2, 4))
    big = 1024
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.ellipse((8, 8, big - 8, big - 8), fill=CHARCOAL)
    draw.ellipse((70, 70, big - 70, big - 70), outline=ring, width=10)
    draw.ellipse((96, 96, big - 96, big - 96), outline=ring, width=3)
    font = ImageFont.truetype(str(ROOT / "assets" / "fonts" / "Marcellus-Regular.ttf"), 520)
    draw.text((big / 2, big / 2 + 20), letter, font=font, fill=BONE if ring == BRASS else ring, anchor="mm")
    buffer = io.BytesIO()
    image.resize((256, 256), Image.LANCZOS).save(buffer, format="PNG")
    return buffer.getvalue()


def data_uri(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode()


def main() -> None:
    cfg = json.loads(CONFIG.read_text(encoding="utf-8")) if CONFIG.exists() else {}
    npcs = {**DEFAULT_NPCS, **{k: tuple(v) for k, v in cfg.get("npcs", {}).items()}}
    redraw = set()
    if "--npc" in sys.argv:
        name, letter, ring = sys.argv[sys.argv.index("--npc") + 1:sys.argv.index("--npc") + 4]
        key = re.sub(r"[^a-z0-9]+", "", name.lower().split(",")[0])[:20] or "npc"
        npcs[key] = (name, letter[:1].upper(), ring.lstrip("#"))
        redraw.add(key)

    game = settings().game_channel
    guild_id = channel_id = None
    for guild in call("GET", "/users/@me/guilds"):
        for channel in call("GET", f"/guilds/{guild['id']}/channels"):
            if channel["type"] == 0 and channel["name"] == game:
                guild_id, channel_id = guild["id"], channel["id"]
    if channel_id is None:
        raise SystemExit(f"No #{game} channel found in the bot's servers.")

    app = call("GET", "/oauth2/applications/@me")["id"]
    existing = {e["name"]: e for e in call("GET", f"/applications/{app}/emojis")["items"]}
    avatars = {}
    for key, (name, letter, ring) in npcs.items():
        emoji_name = f"npc_{key}"
        emoji = existing.get(emoji_name)
        if emoji is not None and key in redraw:
            call("DELETE", f"/applications/{app}/emojis/{emoji['id']}")
            emoji = None
        if emoji is None:
            emoji = call("POST", f"/applications/{app}/emojis", {"name": emoji_name, "image": data_uri(monogram(letter, ring))})
        avatars[key] = f"https://cdn.discordapp.com/emojis/{emoji['id']}.png?size=256"

    hooks = call("GET", f"/channels/{channel_id}/webhooks")
    hook = next((h for h in hooks if h.get("name") == "The Narrator"), None) or call(
        "POST", f"/channels/{channel_id}/webhooks", {"name": "The Narrator", "avatar": data_uri(monogram("N", "c8a15a"))})
    owner = next((a.split("=", 1)[1] for a in sys.argv if a.startswith("--owner=")), cfg.get("owner_id", ""))
    cfg = {"guild_id": guild_id, "channel_id": channel_id, "owner_id": owner, "webhook": f"https://discord.com/api/v10/webhooks/{hook['id']}/{hook['token']}",
           "npcs": {k: list(v) for k, v in npcs.items()}, "avatars": avatars}
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    print(f"DM ready in #{game}: voices {', '.join(sorted(avatars))}")


if __name__ == "__main__":
    main()
