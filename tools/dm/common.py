"""Shared plumbing for the DM kit: the bot token, the game channel, and the saved webhook (data/dm.json)."""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
CONFIG = ROOT / "data" / "dm.json"   # gitignored: holds the webhook URL, which is a secret
API = "https://discord.com/api/v10"

for stream in (sys.stdout, sys.stderr):
    stream.reconfigure(encoding="utf-8", errors="replace")


def settings():
    from psychograph.settings import load_settings

    return load_settings()


def headers(content_type: str | None = "application/json") -> dict:
    result = {"Authorization": f"Bot {settings().discord_token}", "User-Agent": "DiscordBot (psychograph-dm, 1.0)"}
    if content_type:
        result["Content-Type"] = content_type
    return result


def call(method: str, path: str, body=None, raw: bytes | None = None, content_type: str | None = "application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    request = urllib.request.Request(f"{API}{path}", data=data, method=method, headers=headers(content_type))
    with urllib.request.urlopen(request, timeout=30) as response:
        text = response.read()
        return json.loads(text) if text else None


def config() -> dict:
    if not CONFIG.exists():
        raise SystemExit("No DM config yet: run `python tools/dm/setup.py` first.")
    return json.loads(CONFIG.read_text(encoding="utf-8"))
