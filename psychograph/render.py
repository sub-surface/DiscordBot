"""Discord presentation: reply embeds, response splitting, generation stats."""

from __future__ import annotations

from collections.abc import Sequence

import discord

from .backends import Completion

EMBED_COLOR = 0x347A68
RESPONSE_EMBED_LIMIT = 4000


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
) -> discord.Embed:
    embed = discord.Embed(description=text, color=EMBED_COLOR)
    embed.set_author(name=persona_name)
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
) -> tuple[list[str], list[discord.Embed]]:
    """The response's chunks and one embed per chunk; sources and footer go on the first."""
    chunks = split_response(text)
    embeds = [
        response_embed(chunk, persona_name, source_urls if index == 0 else (), footer_parts if index == 0 else ())
        for index, chunk in enumerate(chunks)
    ]
    return chunks, embeds


def board_embed(title: str, description: str, has_image: bool) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=EMBED_COLOR)
    if has_image:
        embed.set_image(url="attachment://board.png")
    return embed
