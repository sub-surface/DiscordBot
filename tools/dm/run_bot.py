"""Run the bot for a game session: game channel only, no heartbeat or digest, and the local backend so no chat
reply can ever wake the Modal GPU. Everything else is the normal .env / config.yaml.

    python tools/dm/run_bot.py            # in the background; stop it to bring the normal bot back

Don't restart it mid-scene: a player halfway through /character create loses the form.
"""

from __future__ import annotations

import dataclasses
import logging
import os

from common import ROOT

os.environ["LLM_BACKEND"] = "local"

from psychograph import actions, bot as botmod  # noqa: E402
from psychograph.settings import load_settings  # noqa: E402

settings = load_settings()
settings = dataclasses.replace(settings, allowed_channels=(settings.game_channel,), heartbeat_channels=(), digest_channel="")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
actions.setup(ROOT / "actions.log")
client = botmod.PsychographBot(settings)
logging.getLogger("psychograph").info("GAME BOT · backend %s · #%s only", client.backend.name, settings.game_channel)
client.run(settings.discord_token, log_handler=None)
