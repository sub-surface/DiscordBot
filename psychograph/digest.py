"""The Monday digest: the past week in the bot's channels, posted to the newsroom by the minutes persona.

A plain post, not a report card: a short editorial in the minutes persona's voice (one model call, when a
model that runs tools is up), the most-reacted messages linked inline, the most-reacted message from
this week last year, and the counts (who talked most, busiest channels, the scoreboards, the bot's own
replies) as subtext underneath. Everything but the editorial is counted, not written.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import discord

from .bot import is_allowed_channel
from .conversation import clip
from .personas import Persona
from .render import VOICE_MESSAGE_LIMIT
from .speak import generate

if TYPE_CHECKING:
    from .bot import PsychographBot

log = logging.getLogger("psychograph.digest")

PER_CHANNEL = 4000      # messages read per channel at most
TOP_MESSAGES = 5
TOP_PEOPLE = 8
QUOTE_CHARS = 110
LAST_YEAR = timedelta(days=364)       # the same weekday, a year back
LAST_YEAR_SPREAD = timedelta(days=3)  # "this week last year": that day, give or take
LAST_YEAR_MIN_REACTIONS = 3


def week_key(now: datetime) -> str:
    year, week, _ = now.isocalendar()
    return f"digest:{year}-W{week:02d}"


def voice(bot: PsychographBot, guild_id: int | None) -> Persona:
    """Who posts the digest: the minutes persona, else the default."""
    return bot.personas.get(guild_id, "minutes") or bot.personas.default()


def _reactions(message: discord.Message) -> int:
    return sum(reaction.count for reaction in message.reactions)


def _quoted(name: str, channel: str, text: str, url: str) -> str:
    return f"**{name}** in #{channel}: [“{clip(text, QUOTE_CHARS)}”](<{url}>)"


async def build(bot: PsychographBot, guild: discord.Guild, now: datetime | None = None) -> str:
    """The digest post, within one message."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(days=7)
    counts: Counter[str] = Counter()
    per_channel: Counter[str] = Counter()
    reacted: list[tuple[int, str, str, str, str]] = []
    channels = [channel for channel in guild.text_channels if is_allowed_channel(channel, bot.settings.allowed_channels)]
    for channel in channels:
        try:
            async for message in channel.history(after=since, limit=PER_CHANNEL):
                if message.author.bot or message.webhook_id:
                    continue
                name = message.author.display_name
                counts[name] += 1
                per_channel[channel.name] += 1
                total = _reactions(message)
                if total and message.clean_content.strip():
                    reacted.append((total, name, channel.name, message.clean_content, message.jump_url))
        except discord.HTTPException:
            log.info("Digest couldn't read #%s", channel.name)

    reacted.sort(key=lambda row: -row[0])
    top = [f"- {_quoted(name, channel, text, url)} · {total}" for total, name, channel, text, url in reacted[:TOP_MESSAGES]]
    people = [f"{name} {count:,}" for name, count in counts.most_common(TOP_PEOPLE)]
    busiest = ", ".join(f"#{name} ({count:,})" for name, count in per_channel.most_common(3))
    week = bot.store.week_counts(guild.id)
    stats = bot.store.generation_stats("-7 days", guild.id)

    opening = await editorial(bot, guild, top, people, busiest) or f"**The week to {now.day} {now.strftime('%B')}**"
    remembered = await last_year(channels, now)
    subtext = [
        f"-# {sum(counts.values()):,} messages · most active: {', '.join(people)} · busiest: {busiest}" if people else "",
        f"-# {week['debates']} debates reviewed · {week['duels']} duels · {week['predictions_logged']} predictions "
        f"logged, {week['predictions_settled']} settled · {stats['replies'] or 0:,} replies from me, "
        f"{stats['cold_starts'] or 0} cold starts, {stats['failures'] or 0} failed",
    ]

    def post(shown: list[str]) -> str:
        parts = [
            opening,
            "\n".join(["**Most reacted**", *shown]) if shown else "",
            f"**This week last year:** {remembered}" if remembered else "",
            "\n".join(line for line in subtext if line),
        ]
        return "\n\n".join(part for part in parts if part)

    while len(post(top)) > VOICE_MESSAGE_LIMIT and top:
        top = top[:-1]  # drop the least-reacted until it fits one message
    return post(top)[:VOICE_MESSAGE_LIMIT]


async def last_year(channels: list, now: datetime) -> str:
    """The most-reacted member message from this week last year in these channels, if any landed."""
    when = now - LAST_YEAR
    best: tuple[int, str] | None = None
    for channel in channels:
        try:
            async for message in channel.history(limit=100, around=when):
                if message.author.bot or message.webhook_id or abs(message.created_at - when) > LAST_YEAR_SPREAD:
                    continue
                total = _reactions(message)
                if total >= LAST_YEAR_MIN_REACTIONS and message.clean_content.strip() and (best is None or total > best[0]):
                    line = _quoted(message.author.display_name, channel.name, message.clean_content, message.jump_url)
                    best = (total, f"{line} · {total}")
        except discord.HTTPException:
            log.info("Digest couldn't look back a year in #%s", channel.name)
    return best[1] if best else ""


async def editorial(bot: PsychographBot, guild: discord.Guild, top: list[str], people: list[str], busiest: str) -> str:
    minutes = bot.personas.get(guild.id, "minutes")
    if minutes is None or not bot.responder.can_run(minutes) or not (top or people):
        return ""
    facts = "\n".join(["Most-reacted messages:", *top, "Most active: " + ", ".join(people), "Busiest channels: " + busiest])
    text = await generate(
        bot,
        minutes,
        "Write this week's Monday digest for the server: under 100 words, deadpan committee-secretary minutes, "
        "built only from the facts above. Name people in bold. No headings, no links, no 'Minutes end.'",
        context=f"[This week's facts, counted by the bot]\n{facts}",
    )
    return text or ""
