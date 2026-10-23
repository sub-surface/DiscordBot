"""Tarot from the 1909 Rider–Waite–Smith deck, read by Mini Me, the seer in the Cringe Tavern's cellar.

The cards are linked from Wikimedia Commons at full resolution (assets/tarot/deck.json); Discord's media proxy
fetches them, so the bot hosts nothing. Readings are Components V2 layouts: a container with the card art in a
media gallery and the meaning underneath, framed in the suit's colour. They're dealt face down (the 1909 back) and
turned one card at a time by editing the message. The day's card is the same for the whole server all day. With
tarot_mini_me on (or /bot tarot on), Mini Me adds a line of her own under the cards: one model call per reading.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .bot import PsychographBot

log = logging.getLogger("psychograph.tarot")

DECK_PATH = Path(__file__).resolve().parent.parent / "assets" / "tarot" / "deck.json"
ACCENT = 0xC8A15A  # brass: the Major Arcana, and a reading not yet turned
SUIT_ACCENTS = {"Wands": 0xC2562E, "Cups": 0x3E7CB1, "Swords": 0x8C9AA6, "Pentacles": 0x5B8A3C}  # ember, sea, steel, moss
FLIP_SECONDS = 1.2  # between one card turning and the next
TAKE_WAIT = 90      # seconds to wait for Mini Me's take: a cold GPU can take a minute
TAKE_COOLDOWN = 60  # per person, so asking for card after card is one model call, not one each
TAKE_CHARS = 500
MINI_ME = ("You are Mini Me, the seer in the cellar of the Cringe Tavern in the Scrollands: a small machine who reads "
           "Rider-Waite-Smith cards for regulars, dry, a little ominous and very online. Read the cards you're given "
           "against the question in one or two short sentences, as you'd say it across the table. No preamble, no "
           "lists, don't recite the meanings back, don't explain tarot.")
REVERSED_CHANCE = 1 / 3
SPREAD = ("What was", "What is", "What's coming")
OPENERS = (
    "Mini Me shuffles with hands that have been banned from three servers.",
    "Mini Me blows dust off the deck. Some of it is not dust.",
    "Mini Me cuts the deck and refuses to say who taught the trick.",
    "Mini Me lays the cards out on a table that is mostly ring stains.",
    "Mini Me shuffles. Somewhere upstairs, a chair is missing again.",
)
TODAY_OPENERS = (
    "The day's card, turned at dawn in the Cringe Tavern cellar. It applies to everyone. Cope accordingly.",
    "Mini Me has drawn for the whole realm today, without asking permission.",
    "The Scrollands' card for today. The Jannies have been notified.",
)
_last_take: dict[int, float] = {}


# A quick reading asked for in chat: "draw a card", "pull me three cards", "tarot", "card of the day", then an
# optional question after a colon, a dash, "about", "for" or "on" ("tarot: will I get the job").
_ASK = re.compile(
    r"^(?:(?:draw|pull|turn)\s+(?:me\s+)?(?:(?P<n>a|one|1|three|3)\s+)?cards?|(?P<three3>three|3)\s+cards|tarot(?:\s+me)?"
    r"|(?P<today>(?:the\s+)?(?:day'?s|today'?s)\s+card|card\s+of\s+the\s+day))(?=\W|$)(?P<rest>.*)$",
    re.IGNORECASE | re.DOTALL,
)
_LEAD = re.compile(r"^\s*(?:[:,;—–-]|(?:about|for|on)\b)\s*", re.IGNORECASE)


def request(text: str, strict: bool = False) -> tuple[str, str] | None:
    """(kind, question) for a message asking for a quick reading, or None.

    strict (a message nobody addressed to the bot): a question has to be set off ("tarot: …", "draw a card for …"),
    so "tarot is cringe" stays chat. Addressed, a question ending in "?" counts without one ("@bot draw a card will
    I win?")."""
    text = " ".join(text.split())
    match = _ASK.match(text)
    if match is None or len(text) > 300:
        return None
    rest = match["rest"].strip()
    lead = _LEAD.match(rest)
    if rest and lead is None and (strict or not rest.endswith("?")):
        return None
    question = rest[lead.end():] if lead else rest
    kind = "today" if match["today"] else "three" if (match["n"] or "").lower() in ("three", "3") or match["three3"] else "one"
    return kind, question.strip()


@lru_cache(maxsize=1)
def _data() -> dict:
    return json.loads(DECK_PATH.read_text(encoding="utf-8"))


def deck() -> list[dict]:
    return _data()["cards"]


def back() -> str:
    """The original 1909 "roses and lilies" back, from the same scans as the faces."""
    return _data()["back"]["image"]


def draw(count: int = 1, rng: random.Random | None = None) -> list[tuple[dict, bool]]:
    """Cards without replacement, each (card, reversed)."""
    rng = rng or random.SystemRandom()
    return [(card, rng.random() < REVERSED_CHANCE) for card in rng.sample(deck(), count)]


def card_of_the_day(guild_id: int | None, day: date | None = None) -> tuple[dict, bool]:
    """The same card for everyone in the server all day: seeded by the guild and the date."""
    day = day or date.today()
    return draw(1, random.Random(f"{guild_id or 0}:{day.isoformat()}"))[0]


def meaning(card: dict, reversed_: bool) -> str:
    return card["reversed" if reversed_ else "upright"]


def card_title(card: dict, reversed_: bool) -> str:
    return f"{card['name']}{', reversed' if reversed_ else ''}"


def accent(cards: list[tuple[dict, bool]]) -> int:
    """The frame's colour: the suit's, brass for the Major Arcana or nothing turned yet. In a spread, a colour two
    cards share wins, else the middle card's (with two turned so far, the newer one's)."""
    colours = [SUIT_ACCENTS.get(card.get("suit"), ACCENT) for card, _ in cards]
    if not colours:
        return ACCENT
    return next((colour for colour in colours if colours.count(colour) > 1), colours[len(colours) // 2])


@dataclass
class Reading:
    """A reading before it's posted, drawable at any point of the deal: `view(shown)` turns the first `shown` cards."""

    heading: str
    cards: list[tuple[dict, bool]]
    labels: tuple[str, ...] = ()
    footer: str = ""
    who: str = ""
    question: str = ""

    def view(self, shown: int | None = None, take: str = "", waiting: bool = False) -> discord.ui.LayoutView:
        shown = len(self.cards) if shown is None else shown
        turned = self.cards[:shown]
        gallery = discord.ui.MediaGallery(*(
            discord.MediaGalleryItem(card["image"], description=card_title(card, rev)[:200]) if index < shown
            else discord.MediaGalleryItem(back(), description="Face down")  # alt text mustn't give the card away
            for index, (card, rev) in enumerate(self.cards)
        ))
        lines = []
        for index, (card, rev) in enumerate(turned):
            label = f"{self.labels[index]} · " if index < len(self.labels) else ""
            lines.append(f"**{label}{card_title(card, rev)}**\n{meaning(card, rev)[:1].upper()}{meaning(card, rev)[1:]}."
                         + ("\n-# Reversed: read it upside down, like the card." if rev and len(self.cards) == 1 else ""))
        children = [discord.ui.TextDisplay(self.heading), gallery,
                    discord.ui.TextDisplay("\n\n".join(lines) or "-# Face down. Mini Me's hand hovers.")]
        if take:
            children.append(discord.ui.TextDisplay(f"> {take}\n> — *Mini Me*"))
        elif waiting and shown == len(self.cards):
            children.append(discord.ui.TextDisplay("-# Mini Me squints at the cards…"))
        if self.footer:
            children.append(discord.ui.TextDisplay(f"-# {self.footer}"))
        view = discord.ui.LayoutView(timeout=None)
        view.add_item(discord.ui.Container(*children, accent_colour=accent(turned)))
        return view


def reading(kind: str, guild_id: int | None, who: str, question: str = "", rng: random.Random | None = None) -> Reading:
    """"today" (the server's card of the day), "one" (a card for you) or "three" (what was, is, and is coming)."""
    rng = rng or random.SystemRandom()
    who = discord.utils.escape_mentions(discord.utils.escape_markdown(who))
    question = discord.utils.escape_mentions(discord.utils.escape_markdown(" ".join(question.split())[:200]))
    asked = f"\n-# {who} asks: *{question}*" if question else ""  # escaped too, in case pings are ever allowed
    if kind == "today":
        return Reading(f"### The day's card\n{rng.choice(TODAY_OPENERS)}", [card_of_the_day(guild_id)],
                       footer=f"{date.today():%A %d %B} · the same card for everyone until midnight", who=who)
    if kind == "three":
        return Reading(f"### A reading for {who}\n{rng.choice(OPENERS)}{asked}", draw(3, rng), SPREAD,
                       footer="Rider–Waite–Smith, 1909. Mini Me accepts tips in keks.", who=who, question=question)
    return Reading(f"### A card for {who}\n{rng.choice(OPENERS)}{asked}", draw(1, rng),
                   footer="Rider–Waite–Smith, 1909. Mini Me accepts tips in keks.", who=who, question=question)


# ── Mini Me's take: an optional line from the model under the cards (tarot_mini_me, or /bot tarot) ──

def mini_me_on(bot: PsychographBot) -> bool:
    state = bot.store.state("tarot:mini_me")
    return state == "on" if state else bot.settings.tarot_mini_me


def wants_take(bot: PsychographBot, user_id: int | None) -> bool:
    """Whether this reading gets Mini Me's take: switched on, and not this person's second within TAKE_COOLDOWN,
    so asking for card after card doesn't mean a model call each."""
    if not mini_me_on(bot) or user_id is None:
        return False
    now = time.monotonic()
    if now - _last_take.get(user_id, -TAKE_COOLDOWN) < TAKE_COOLDOWN:
        return False
    _last_take[user_id] = now
    return True


async def take(bot: PsychographBot, reading: Reading) -> str:
    """One or two sentences from Mini Me tying the cards to the question; "" if the model failed or said nothing."""
    from . import conversation  # imported here, as dice.py does: keeps tarot's own imports light

    cards = "; ".join(
        f"{reading.labels[index] + ': ' if index < len(reading.labels) else ''}{card_title(card, rev)} ({meaning(card, rev)})"
        for index, (card, rev) in enumerate(reading.cards)
    )
    messages = [
        {"role": "system", "content": MINI_ME},
        {"role": "user", "content": f"{reading.who or 'The whole tavern'} asks: {reading.question or 'nothing in particular'}\n"
                                    f"The cards: {cards}"},
    ]
    try:
        completion = await bot.backend.complete(messages, thinking=False)
    except Exception:
        log.exception("Mini Me's take failed")
        return ""
    bot.model_warmed()
    text = conversation.clean_reply(completion.text, "Mini Me", ())
    return discord.utils.escape_mentions(" ".join(text.split()))[:TAKE_CHARS]


async def deal(reading: Reading, send: Callable[[discord.ui.LayoutView], Awaitable[object]],
               edit: Callable[[discord.ui.LayoutView], Awaitable[object]], take: Awaitable[str] | None = None) -> None:
    """Post `reading` face down and turn the cards one at a time; then, if `take` (Mini Me's line, already asked
    for), add it under them when it arrives. A deleted message or a failed edit just ends the deal."""
    pending = asyncio.ensure_future(take) if take is not None else None
    try:
        await send(reading.view(0))
        for shown in range(1, len(reading.cards) + 1):
            await asyncio.sleep(FLIP_SECONDS)
            await edit(reading.view(shown, waiting=pending is not None))
        if pending is not None:
            try:
                line = await asyncio.wait_for(pending, TAKE_WAIT)
            except asyncio.TimeoutError:
                line = ""
            await edit(reading.view(take=line))  # also clears "squints at the cards" when there's nothing
    except discord.HTTPException as error:
        log.info("Tarot deal stopped: %s", error)
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
