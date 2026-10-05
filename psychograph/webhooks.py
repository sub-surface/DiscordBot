"""Persona identities: speaking through a webhook with the persona's name and avatar, and hosting avatars.

Needs the bot's Manage Webhooks permission; without it, replies fall back to embeds.

Impersonation guard: a persona may not be named after a member of the server, and if a member
later takes a persona's name, that persona's messages are marked "(persona)".
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from io import BytesIO

import discord

from .personas import Persona

log = logging.getLogger("psychograph.webhooks")

WEBHOOK_NAME = "Psychograph personas"
AVATAR_MAX_BYTES = 8 * 1024 * 1024
AVATAR_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
AVATAR_SIZE = 256
MEMBER_CHECK_SECONDS = 600


def webhook_username(name: str) -> str:
    """Discord rejects webhook names containing "discord" or "clyde", and caps them at 80 characters."""
    cleaned = re.sub(r"discord|clyde", lambda match: match.group(0)[0] + "​" + match.group(0)[1:], name, flags=re.IGNORECASE)
    return (cleaned.strip() or "persona")[:80]


def square_png(data: bytes, size: int = AVATAR_SIZE) -> bytes:
    """Centre-crop an uploaded image to a square PNG avatar. Raises ValueError for anything that isn't one."""
    from PIL import Image, ImageOps

    try:
        image = Image.open(BytesIO(data))
        if image.width * image.height > 40_000_000:
            raise ValueError("That image is too large.")
        image.load()
    except ValueError:
        raise
    except Exception as error:  # Pillow raises many types for bad input
        raise ValueError("That file isn't an image I can read.") from error
    image = ImageOps.fit(ImageOps.exif_transpose(image).convert("RGBA"), (size, size), Image.Resampling.LANCZOS)
    output = BytesIO()
    image.save(output, "PNG", optimize=True)
    return output.getvalue()


async def member_named(guild: discord.Guild, name: str) -> bool:
    """Whether a member's username, global name, nickname or display name is exactly `name` (any case)."""
    wanted = name.strip().casefold()
    if not wanted:
        return False
    members: list[discord.Member] = []
    cached = guild.get_member_named(name)
    if cached:
        members.append(cached)
    try:
        # A prefix search over the gateway; works without the privileged members intent.
        members.extend(await guild.query_members(query=name.strip()[:100], limit=25, cache=False))
    except (discord.HTTPException, discord.ClientException, asyncio.TimeoutError):
        log.info("Couldn't search members of guild %s", guild.id)
    return any(
        wanted in {value.casefold() for value in (member.name, member.global_name, member.nick, member.display_name) if value}
        for member in members
    )


class PersonaWebhooks:
    def __init__(self, client: discord.Client) -> None:
        self.client = client
        self._cache: dict[int, discord.Webhook] = {}
        self._member_names: dict[tuple[int, str], tuple[bool, float]] = {}

    async def username(self, channel: discord.abc.Messageable, persona: Persona) -> str:
        """The persona's name, marked "(persona)" if a member of this server goes by it."""
        name = webhook_username(persona.name)
        guild = getattr(channel, "guild", None)
        if guild is None:
            return name
        key = (guild.id, name.casefold())
        taken, checked = self._member_names.get(key, (False, 0.0))
        if time.monotonic() - checked > MEMBER_CHECK_SECONDS:
            taken = await member_named(guild, persona.name)
            self._member_names[key] = (taken, time.monotonic())
        return webhook_username(f"{persona.name[:68]} (persona)") if taken else name

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
        username = await self.username(channel, persona)
        for attempt in range(2):
            webhook = await self._webhook(base)
            try:
                return await webhook.send(
                    content,
                    username=username,
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

    async def host_avatar(
        self, channel: discord.abc.Messageable, persona: Persona, png: bytes, existing_webhook_id: int | None
    ) -> tuple[str, int]:
        """Store `png` as the avatar of a small holder webhook; returns its permanent URL and the webhook id.

        Attachment links expire, but webhook avatars stay up for as long as the webhook exists.
        Re-uploading edits the persona's existing holder instead of creating another.
        """
        base = self._base(channel)
        if base is None:
            raise discord.ClientException("Avatars can only be uploaded in a server text channel.")
        name = webhook_username(f"avatar · {persona.name}")
        holder: discord.Webhook | None = None
        if existing_webhook_id:
            try:
                holder = await (await self.client.fetch_webhook(existing_webhook_id)).edit(
                    name=name, avatar=png, reason="Psychograph persona avatar"
                )
            except discord.HTTPException:
                holder = None  # gone; make a new one
        if holder is None:
            holder = await base.create_webhook(name=name, avatar=png, reason="Psychograph persona avatar")
        if holder.avatar is None:
            raise discord.ClientException("Discord didn't keep the avatar.")
        return holder.avatar.with_format("png").with_size(AVATAR_SIZE).url, holder.id

    async def drop_avatar(self, webhook_id: int | None) -> None:
        if not webhook_id:
            return
        try:
            await (await self.client.fetch_webhook(webhook_id)).delete(reason="Psychograph persona avatar removed")
        except discord.HTTPException:
            log.info("Avatar holder webhook %s was already gone", webhook_id)

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
