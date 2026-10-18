"""Tarot from the 1909 Rider–Waite–Smith deck, read by Mini Me, the seer in the Cringe Tavern's cellar.

The cards are linked from Wikimedia Commons at full resolution (assets/tarot/deck.json); Discord's media proxy
fetches them, so the bot hosts nothing. Readings are Components V2 layouts: a container with the card art in a
media gallery and the meaning underneath. The day's card is the same for the whole server all day.
"""

from __future__ import annotations

import json
import random
from datetime import date
from functools import lru_cache
from pathlib import Path

import discord

DECK_PATH = Path(__file__).resolve().parent.parent / "assets" / "tarot" / "deck.json"
ACCENT = 0xC8A15A  # brass
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


@lru_cache(maxsize=1)
def deck() -> list[dict]:
    return json.loads(DECK_PATH.read_text(encoding="utf-8"))["cards"]


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


def layout(heading: str, cards: list[tuple[dict, bool]], labels: tuple[str, ...] = (), footer: str = "") -> discord.ui.LayoutView:
    """A reading as a Components V2 message: heading, the card art, then a line per card."""
    view = discord.ui.LayoutView(timeout=None)
    gallery = discord.ui.MediaGallery(*(
        discord.MediaGalleryItem(card["image"], description=card_title(card, rev)[:200]) for card, rev in cards
    ))
    lines = []
    for index, (card, rev) in enumerate(cards):
        label = f"{labels[index]} · " if index < len(labels) else ""
        lines.append(f"**{label}{card_title(card, rev)}**\n{meaning(card, rev)[:1].upper()}{meaning(card, rev)[1:]}."
                     + ("\n-# Reversed: read it upside down, like the card." if rev and len(cards) == 1 else ""))
    children = [discord.ui.TextDisplay(heading), gallery, discord.ui.TextDisplay("\n\n".join(lines))]
    if footer:
        children.append(discord.ui.TextDisplay(f"-# {footer}"))
    view.add_item(discord.ui.Container(*children, accent_colour=ACCENT))
    return view


def reading(kind: str, guild_id: int | None, who: str, question: str = "", rng: random.Random | None = None) -> discord.ui.LayoutView:
    """"today" (the server's card of the day), "one" (a card for you) or "three" (what was, is, and is coming)."""
    rng = rng or random.SystemRandom()
    asked = f"\n-# {who} asks: *{question.strip()[:200]}*" if question.strip() else ""
    if kind == "today":
        return layout(f"### The day's card\n{rng.choice(TODAY_OPENERS)}", [card_of_the_day(guild_id)],
                      footer=f"{date.today():%A %d %B} · the same card for everyone until midnight")
    if kind == "three":
        return layout(f"### A reading for {who}\n{rng.choice(OPENERS)}{asked}", draw(3, rng), SPREAD,
                      footer="Rider–Waite–Smith, 1909. Mini Me accepts tips in keks.")
    return layout(f"### A card for {who}\n{rng.choice(OPENERS)}{asked}", draw(1, rng),
                  footer="Rider–Waite–Smith, 1909. Mini Me accepts tips in keks.")
