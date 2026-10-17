"""Images people post, shrunk and inlined for a vision model.

The bot downloads each image itself (Discord CDN links are signed and expire, and the Modal worker
shouldn't need network access to Discord), fits it within MAX_SIDE, and sends it as a JPEG data URL
in an OpenAI-style `image_url` part on the final user turn.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
from collections.abc import Sequence
from urllib.parse import urlsplit

import aiohttp
import discord

from .conversation import IMAGE_PART_TOKENS

log = logging.getLogger("psychograph.vision")

MAX_IMAGES = 3
MAX_SIDE = 1024
MAX_BYTES = 12 * 1024 * 1024
# Prompt tokens held back per image when fitting the context: MiniCPM-V's default 16x mode measured ~200
# for a 640x400 picture, and larger images slice into more tiles.
IMAGE_TOKENS = IMAGE_PART_TOKENS
_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp")
DISCORD_MEDIA_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net", "images-ext-1.discordapp.net", "images-ext-2.discordapp.net"})
# Fixed hosts the bot may also fetch from: linked posts' photos, read from their public record.
MEDIA_HOSTS = DISCORD_MEDIA_HOSTS | {"pbs.twimg.com"}


def _is_image(attachment: discord.Attachment) -> bool:
    kind = (attachment.content_type or "").split(";")[0]
    return kind.startswith("image/") or attachment.filename.lower().endswith(_IMAGE_SUFFIXES)


def image_urls(*messages: discord.Message | None) -> list[str]:
    """Image attachments and embedded pictures in these messages, in order, at most MAX_IMAGES."""
    urls: list[str] = []
    for message in messages:
        if message is None:
            continue
        urls.extend(item.url for item in getattr(message, "attachments", ()) if _is_image(item))
        for embed in getattr(message, "embeds", ()):
            # A posted image link unfurls as an image (or gifv) embed; link previews only get a thumbnail.
            # Only Discord's proxied copy: the original URL is wherever the poster pointed it.
            picture = embed.image.proxy_url or (embed.thumbnail.proxy_url if embed.type in ("image", "gifv") else None)
            if picture:
                urls.append(picture)
    return [url for url in dict.fromkeys(urls) if _hosted(url, DISCORD_MEDIA_HOSTS)][:MAX_IMAGES]


def _hosted(url: str, hosts: frozenset[str]) -> bool:
    """The bot only fetches from fixed media hosts over https, so a posted link can't aim it elsewhere."""
    parts = urlsplit(url)
    return parts.scheme == "https" and parts.hostname in hosts


def _shrink(data: bytes) -> str:
    from PIL import Image

    with Image.open(io.BytesIO(data)) as image:
        image.seek(0)  # first frame of a GIF
        frame = image.convert("RGB")
    frame.thumbnail((MAX_SIDE, MAX_SIDE))
    buffer = io.BytesIO()
    frame.save(buffer, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode()


async def _fetch(session: aiohttp.ClientSession, url: str) -> str | None:
    if not _hosted(url, MEDIA_HOSTS):
        return None
    try:
        async with session.get(url, allow_redirects=False) as response:
            if response.status != 200 or (response.content_length or 0) > MAX_BYTES:
                return None
            data = bytearray()
            async for chunk in response.content.iter_chunked(1 << 16):
                data += chunk
                if len(data) > MAX_BYTES:
                    return None
        return await asyncio.to_thread(_shrink, data)
    except Exception as error:  # a broken or unsupported image is skipped, not fatal
        log.info("Skipped image %s: %s", url.split("?")[0], error)
        return None


async def inline(urls: Sequence[str]) -> list[str]:
    """Data URLs for the images that downloaded and decoded."""
    if not urls:
        return []
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        results = await asyncio.gather(*(_fetch(session, url) for url in urls))
    return [result for result in results if result]


def attach(message: dict, images: Sequence[str]) -> None:
    """Turn a text message into text + image parts, in place."""
    message["content"] = [
        {"type": "text", "text": str(message["content"])},
        *({"type": "image_url", "image_url": {"url": url}} for url in images),
    ]
