"""Speaking as a persona: one bot-owned webhook per channel, posting with the persona's name and avatar.

Needs the bot's Manage Webhooks permission; without it, replies fall back to embeds.
"""

from __future__ import annotations

import logging
import re

import discord

from .personas import Persona

log = logging.getLogger("psychograph.webhooks")

WEBHOOK_NAME = "Psychograph personas"


def webhook_username(name: str) -> str:
    """Discord rejects webhook names containing "discord" or "clyde", and caps them at 80 characters."""
    cleaned = re.sub(r"discord|clyde", lambda match: match.group(0)[0] + "​" + match.group(0)[1:], name, flags=re.IGNORECASE)
    return (cleaned.strip() or "persona")[:80]


class PersonaWebhooks:
    def __init__(self, client: discord.Client) -> None:
        self.client = client
        self._cache: dict[int, discord.Webhook] = {}

    @staticmethod
    def _base(channel: discord.abc.Messageable) -> discord.TextChannel | None:
        base = channel.parent if isinstance(channel, discord.Thread) else channel
        return base if isinstance(base, discord.TextChannel) else None

    def available(self, channel: discord.abc.Messageable) -> bool:
        base = self._base(channel)
        return bool(base and base.permissions_for(base.guild.me).manage_webhooks)

    async def _webhook(self, base: discord.TextChannel) -> discord.Webhook:
        cached = self._cache.get(base.id)
        if cached:
            return cached
        webhook = next(
            (
                hook
                for hook in await base.webhooks()
                if hook.user and self.client.user and hook.user.id == self.client.user.id and hook.name == WEBHOOK_NAME
            ),
            None,
        ) or await base.create_webhook(name=WEBHOOK_NAME, reason="Psychograph persona voices")
        self._cache[base.id] = webhook
        return webhook

    async def send(
        self,
        channel: discord.abc.Messageable,
        persona: Persona,
        content: str,
        allowed_mentions: discord.AllowedMentions,
    ) -> discord.WebhookMessage | None:
        """Post as the persona, or return None when this channel can't host a webhook."""
        base = self._base(channel)
        if base is None or not self.available(channel):
            return None
        kwargs = {"thread": channel} if isinstance(channel, discord.Thread) else {}
        for attempt in range(2):
            webhook = await self._webhook(base)
            try:
                return await webhook.send(
                    content,
                    username=webhook_username(persona.name),
                    avatar_url=persona.avatar_url,
                    allowed_mentions=allowed_mentions,
                    wait=True,
                    **kwargs,
                )
            except discord.NotFound:
                self._cache.pop(base.id, None)  # deleted by someone; make a new one once
                if attempt:
                    raise
        return None

    async def delete(self, channel: discord.abc.Messageable, message_id: int) -> bool:
        base = self._base(channel)
        webhook = self._cache.get(base.id) if base else None
        if webhook is None:
            return False
        try:
            kwargs = {"thread": channel} if isinstance(channel, discord.Thread) else {}
            await webhook.delete_message(message_id, **kwargs)
            return True
        except discord.HTTPException:
            return False
