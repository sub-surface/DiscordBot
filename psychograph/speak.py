"""One-off persona lines outside a conversation: duels, heartbeat drop-ins, the Monday digest.

`generate` asks the model for a single in-character message (no reply chain); `speak` posts it as the
persona, through its webhook (own name and avatar) when the bot can, else as an embed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import TYPE_CHECKING

import discord

from . import conversation, render
from .personas import Persona

if TYPE_CHECKING:
    from .bot import PsychographBot

log = logging.getLogger("psychograph.speak")


async def generate(
    bot: PsychographBot, persona: Persona, instruction: str, context: str = "", speakers: Sequence[str] = ()
) -> str | None:
    """A single message from `persona`; None if the model failed or said nothing."""
    system = conversation.system_prompt(persona, "concise", compact=bot.backend.profile.compact)
    prompt = "\n\n".join(part for part in (context, instruction) if part)
    try:
        completion = await bot.backend.complete([{"role": "system", "content": system}, {"role": "user", "content": prompt}])
    except Exception:
        log.exception("Model call failed for %s", persona.name)
        return None
    return conversation.clean_reply(completion.text, persona.name, speakers) or None


async def speak(bot: PsychographBot, channel: discord.abc.Messageable, persona: Persona, text: str) -> discord.Message | None:
    """Post `text` as `persona`."""
    try:
        if bot.webhooks.available(channel):
            message = await bot.webhooks.send(channel, persona, text[: render.VOICE_MESSAGE_LIMIT], discord.AllowedMentions.none())
            if message is not None:
                return message
        embed = render.response_embed(text[: render.RESPONSE_EMBED_LIMIT], persona.name, icon_url=persona.avatar_url)
        return await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        log.info("Couldn't post as %s in channel %s", persona.name, getattr(channel, "id", "?"))
        return None
