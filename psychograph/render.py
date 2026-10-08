"""Discord presentation: reply embeds, response splitting, generation stats."""

from __future__ import annotations

from collections.abc import Sequence

import discord
from discord import app_commands

from .backends import Completion
from .personas import GROUP_ICONS, Persona

EMBED_COLOR = 0x347A68
RESPONSE_EMBED_LIMIT = 4000
VOICE_MESSAGE_LIMIT = 1900  # plain messages cap at 2,000; leave room for a subtext line
CHOICE_LIMIT = 25           # Discord's cap on autocomplete choices and select options
PERSONA_LEGEND = " · ".join(f"{icon} {group}" for group, icon in GROUP_ICONS.items())


def persona_choices(personas: Sequence[Persona], current: str | None = None) -> list[app_commands.Choice[str]]:
    """Autocomplete choices in group order, each with its group's icon; the channel's current one is marked."""
    return [
        app_commands.Choice(name=f"{persona.label}{' · current' if persona.key == current else ''}"[:100], value=persona.key)
        for persona in personas[:CHOICE_LIMIT]
    ]


def split_response(text: str, limit: int = RESPONSE_EMBED_LIMIT) -> list[str]:
    """Split at newlines (or spaces) so every chunk fits one embed; joining the chunks restores the text."""
    chunks = []
    remaining = text.strip()
    while len(remaining) > limit:
        split_at = remaining.rfind("\n", 0, limit)
        if split_at < limit // 2:
            split_at = remaining.rfind(" ", 0, limit)
        if split_at < limit // 2:
            split_at = limit
        cut_at = split_at + 1 if split_at < limit and remaining[split_at] in " \n" else split_at
        chunks.append(remaining[:cut_at])
        remaining = remaining[cut_at:]
    if remaining:
        chunks.append(remaining)
    return chunks or [""]


def model_summary(backend) -> str:
    """What the bot runs on, for /model and the instant answer to "what model are you?"."""
    return (
        f"Backend: **{backend.name}**\nModel: `{backend.label}`\nProfile: {backend.profile.describe()}\n"
        f"Sampling: temperature {backend.temperature}, top-p {backend.top_p}\n{backend.note}"
    )


def generation_summary(completion: Completion, wall_seconds: float) -> str:
    """e.g. "80 tokens · 0.5s · 160.0 tok/s (llama.cpp)"; "~" marks estimates."""
    seconds = completion.seconds if completion.seconds is not None else wall_seconds
    if completion.tokens is None:
        tokens = (len(completion.text.encode("utf-8")) + 2) // 3
        rate = tokens / max(seconds, 0.001)
        return f"~{tokens} tokens · {seconds:.1f}s · ~{rate:.1f} tok/s (estimated)"
    if completion.tokens_per_second is not None:
        return f"{completion.tokens} tokens · {seconds:.1f}s · {completion.tokens_per_second:.1f} tok/s (llama.cpp)"
    rate = completion.tokens / max(seconds, 0.001)
    return f"{completion.tokens} tokens · {seconds:.1f}s · {rate:.1f} tok/s (measured)"


def response_embed(
    text: str,
    persona_name: str,
    source_urls: Sequence[str] = (),
    footer_parts: Sequence[str | None] = (),
    icon_url: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(description=text, color=EMBED_COLOR)
    embed.set_author(name=persona_name, icon_url=icon_url or None)
    if source_urls:
        embed.add_field(name="Referenced posts", value="\n".join(source_urls[:3]), inline=False)
    footer = " · ".join(part for part in footer_parts if part)
    if footer:
        embed.set_footer(text=footer)
    return embed


def response_embeds(
    text: str,
    persona_name: str,
    source_urls: Sequence[str],
    footer_parts: Sequence[str | None],
    icon_url: str | None = None,
) -> tuple[list[str], list[discord.Embed]]:
    """The response's chunks and one embed per chunk; sources and footer go on the first."""
    chunks = split_response(text)
    embeds = [
        response_embed(
            chunk, persona_name, source_urls if index == 0 else (), footer_parts if index == 0 else (), icon_url
        )
        for index, chunk in enumerate(chunks)
    ]
    return chunks, embeds


def voice_messages(text: str, source_urls: Sequence[str], notice: str | None) -> tuple[list[str], list[str]]:
    """Plain chunks for speaking as a persona, and the posted messages (sources/notice as subtext on the last)."""
    chunks = split_response(text, VOICE_MESSAGE_LIMIT)
    extras = [f"-# {notice}"] if notice else []
    extras += [f"-# <{url}>" for url in source_urls[:3]]
    posted = list(chunks)
    if extras:
        posted[-1] = "\n".join([posted[-1], *extras])
    return chunks, posted


def board_embed(title: str, description: str, has_image: bool) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=EMBED_COLOR)
    if has_image:
        embed.set_image(url="attachment://board.png")
    return embed
