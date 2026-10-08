"""Ambient behaviour: noticing messages nobody sent to the bot.

  by name     a chatter persona answers to "<name>-bot" ("hpcr-bot what", "aura-bot thoughts?"), since its bare
              name means the real person; a character answers to its bare name ("mochi would hate this") when Jev
              is sure it's spoken to, unless a member also goes by that name. The bot's own name reaches the
              channel's persona the same way.
  reactions   now and then the channel's persona reacts to a message that lands with a server emote in the mood
              that fits (funny, thinking, baffled, roast…, from emotes.json): Jev picks the mood, and the emote is
              drawn at random from it, favouring the persona's favourites, so the same moment doesn't always get
              the same emote. At most one reaction per channel per cooldown, and Jev is asked about a channel at
              most once a minute. The first message in a channel that's been quiet for four days gets the revival
              emote, and the bot shows a random "thinking" emote while it works on an answer.

Each notice costs at most one Jev call (no model, no GPU), made only after the cheap checks pass.
"""

from __future__ import annotations

import json
import logging
import math
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import discord

from . import conversation
from .jev import AMBIENT_MESSAGES, NAMED, REACTS
from .personas import Persona
from .responder import recent_messages

if TYPE_CHECKING:
    from .bot import PsychographBot

log = logging.getLogger("psychograph.ambient")

ASK_GAP_SECONDS = 60          # Jev is asked whether to react at most this often per channel
MIN_WORDS = 3                 # shorter messages ("lol", "real") aren't reacted to, unless they carry a picture
NAMES_TTL_SECONDS = 60        # the callable persona names are re-read from disk this often
MIN_NAME_CHARS = 3
CHATTER_SUFFIX = "-bot"       # chatters share names with members, so they answer to "<name>-bot"
REVIVAL_AFTER = timedelta(days=4)
FAVOURITE_WEIGHT = 3          # a persona's favourite emotes are drawn this many times as often


@dataclass(frozen=True)
class Mood:
    when: str                 # what kind of message it fits, for Jev
    emotes: tuple[str, ...]   # server emoji names that express it


@dataclass(frozen=True)
class Catalogue:
    moods: dict[str, Mood] = field(default_factory=dict)
    status: str = ""          # the mood shown while the bot thinks
    revival: str = ""         # the emote for the first message after four quiet days


def load_emotes(path: Path) -> Catalogue:
    """emotes.json: {"moods": {name: {"when", "emotes"}}, "status": mood, "revival": emote}. Missing means none."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.info("No emote catalogue at %s", path)
        return Catalogue()
    moods = {
        str(name): Mood(str(mood.get("when", "")), tuple(str(emote) for emote in mood.get("emotes", ())))
        for name, mood in (data.get("moods") or {}).items()
    }
    return Catalogue(moods, str(data.get("status") or ""), str(data.get("revival") or ""))


def call_names(persona: Persona) -> list[str]:
    """What a persona answers to without an @."""
    name = persona.name.casefold()
    return [f"{name}{CHATTER_SUFFIX}"] if persona.group == "chatters" else [name]


def first_named(text: str, names: dict[str, Persona]) -> tuple[str, Persona] | None:
    """The name (and its persona) that comes first in `text`, as a whole word."""
    lowered = text.casefold()
    found = [
        (match.start(), -len(name), name, persona)
        for name, persona in names.items()
        if (match := re.search(rf"(?<![\w-]){re.escape(name)}(?![\w-])", lowered))
    ]
    if not found:
        return None
    *_, name, persona = min(found, key=lambda item: item[:2])
    return name, persona


class Ambient:
    def __init__(self, bot: PsychographBot, clock=time.monotonic, rng: random.Random | None = None) -> None:
        self.bot = bot
        self.clock = clock
        self.rng = rng or random.Random()
        self.catalogue = load_emotes(bot.settings.emotes_file)
        self._reacted: dict[int, float] = {}       # channel id → when its persona last reacted
        self._asked: dict[int, float] = {}         # channel id → when Jev was last asked about reacting there
        self._last_seen: dict[int, datetime] = {}  # channel id → when its latest message was posted
        self._names: dict[int | None, tuple[float, list[Persona]]] = {}

    # ── By name ─────────────────────────────────────────────────────

    def _callable(self, guild_id: int | None) -> list[Persona]:
        cached = self._names.get(guild_id)
        if cached is None or self.clock() - cached[0] > NAMES_TTL_SECONDS:
            personas = [
                persona for persona in self.bot.personas.available(guild_id)
                if persona.mode == "chat" and persona.group != "tools" and len(persona.name) >= MIN_NAME_CHARS
            ]
            cached = self._names[guild_id] = (self.clock(), personas)
        return cached[1]

    async def called(self, message: discord.Message) -> Persona | None:
        """The persona a message calls by name: "<chatter>-bot" always, a character's or the bot's name when Jev
        is sure it's spoken to."""
        guild = message.guild
        guild_id = guild.id if guild else None
        names = {name: persona for persona in self._callable(guild_id) for name in call_names(persona)}
        channel_persona = self.bot.personas.for_channel(message.channel.id, guild_id)
        bot_name = (getattr(getattr(guild, "me", None), "display_name", None) or "").casefold()
        if len(bot_name) >= MIN_NAME_CHARS and channel_persona.mode == "chat":
            names[bot_name] = channel_persona
        found = first_named(message.content or "", names)
        if found is None:
            return None
        name, persona = found
        if name.endswith(CHATTER_SUFFIX) and persona.group == "chatters":
            return persona  # "aura-bot" can only mean the bot
        if name != bot_name and guild is not None and await self.bot.webhooks.member_has_name(guild, persona.name):
            return None  # a character named like a member: there it means the member
        item = conversation.said(message)
        if item is None:
            return None
        before = await recent_messages(message.channel, before=message, limit=AMBIENT_MESSAGES)
        sure = await self.bot.jev.addressed(item.text, item.speaker, persona.name, before)
        log.info("Named %s in channel %s: %.0f%% spoken to", persona.name, message.channel.id, sure * 100)
        return persona if sure >= NAMED else None

    # ── Reactions ───────────────────────────────────────────────────

    @staticmethod
    def _server(guild: discord.Guild | None) -> dict[str, discord.Emoji]:
        return {
            emoji.name.casefold(): emoji for emoji in getattr(guild, "emojis", ()) or () if getattr(emoji, "available", True)
        }

    def moods(self, guild: discord.Guild | None) -> dict[str, list[discord.Emoji]]:
        """Each mood's emotes that this server actually has; moods with none are left out."""
        have = self._server(guild)
        found = {name: [have[e.casefold()] for e in mood.emotes if e.casefold() in have] for name, mood in self.catalogue.moods.items()}
        return {name: emotes for name, emotes in found.items() if emotes}

    def draw(self, emotes: list[discord.Emoji], persona: Persona | None = None) -> discord.Emoji:
        """One emote of a mood at random, the persona's favourites more often."""
        favourites = {name.casefold() for name in (persona.emotes if persona else ())}
        weights = [FAVOURITE_WEIGHT if emoji.name.casefold() in favourites else 1 for emoji in emotes]
        return self.rng.choices(emotes, weights=weights)[0]

    def thinking(self, guild: discord.Guild | None) -> discord.Emoji | None:
        """A random emote from the status mood, to show while the bot works on an answer."""
        emotes = self.moods(guild).get(self.catalogue.status)
        return self.draw(emotes) if emotes else None

    async def seen(self, message: discord.Message) -> datetime | None:
        """Note a message in a channel the bot reads; returns when the one before it was posted, if known."""
        channel_id, posted = message.channel.id, getattr(message, "created_at", None)
        previous = self._last_seen.get(channel_id)
        if previous is None and posted is not None:
            before = await recent_messages(message.channel, before=message, limit=1)  # once per channel per run
            previous = before[-1].created_at if before else None
        if posted is not None:
            self._last_seen[channel_id] = posted
        return previous

    async def react(self, message: discord.Message, previous: datetime | None = None) -> bool:
        """Maybe react to a message nobody sent the bot, as the channel's persona. Returns whether it did."""
        channel_id = message.channel.id
        if not self.bot.settings.ambient_reactions or not self.bot.store.channel_settings(channel_id).reactions:
            return False
        posted = getattr(message, "created_at", None)
        revival = self._server(message.guild).get(self.catalogue.revival.casefold())
        if previous is not None and posted is not None and posted - previous > REVIVAL_AFTER and revival is not None:
            return await self._add(message, revival, "revival")  # four quiet days, then someone speaks

        now = self.clock()
        if now - self._reacted.get(channel_id, -math.inf) < self.bot.settings.ambient_cooldown_minutes * 60:
            return False
        if now - self._asked.get(channel_id, -math.inf) < ASK_GAP_SECONDS or not self.bot.jev.enabled:
            return False
        moods = self.moods(message.guild)
        item = conversation.said(message)
        short = len((message.content or "").split()) < MIN_WORDS and not getattr(message, "attachments", None)
        if not moods or item is None or short:
            return False
        guild_id = message.guild.id if message.guild else None
        persona = self.bot.personas.for_channel(channel_id, guild_id)
        if persona.mode != "chat" or persona.group == "tools":
            return False
        self._asked[channel_id] = now
        before = await recent_messages(message.channel, before=message, limit=AMBIENT_MESSAGES)
        favourites = {name.casefold() for name in persona.emotes}
        choices = {
            name: self.catalogue.moods[name].when
            + (f" A favourite of {persona.name}." if any(e.name.casefold() in favourites for e in emotes) else "")
            for name, emotes in moods.items()
        }
        score, mood = await self.bot.jev.react(item.text, item.speaker, persona, before, choices)
        if score < REACTS or mood not in moods:
            return False
        return await self._add(message, self.draw(moods[mood], persona), f"{persona.name}, {mood} {score:.0%}")

    async def _add(self, message: discord.Message, emoji: discord.Emoji, why: str) -> bool:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException as error:
            log.info("Couldn't react in channel %s: %s", message.channel.id, error)
            return False
        self._reacted[message.channel.id] = self.clock()
        log.info("Reacted :%s: in channel %s (%s)", emoji.name, message.channel.id, why)
        return True
