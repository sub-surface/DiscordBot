"""Halloween: the season (24-31 October) and the Night (the 31st), switched on and off by the calendar alone.

In season:   haunting (now and then a ghost reacts to a message and is gone ten seconds later); "trick or treat"
             once a day each (keks or candy into a Scrollands pack, or spiders on your next few messages, or the
             Pumpkin takes a kek); tarot frames go pumpkin.
On the Night (more of it, and worse):
             the day's card is Death; demons wander out of #nanospasm into the other channels; a channel's
             persona is sometimes possessed by another; old lines from a channel's own history flicker up and
             delete themselves; natural 1s and 20s are witnessed; only Xenodemons answer a summoning, on an
             orange numogram; haunting is four times as likely.

`halloween: auto` in config.yaml follows the date; "on" forces the Night (for testing), "off" turns it all off.
No model anywhere.
"""

from __future__ import annotations

import random
import re
from datetime import date

PUMPKIN = 0xE8751A
SEASON_START = 24  # October
_override = "auto"


def configure(mode: str) -> None:
    global _override
    mode = {"true": "on", "false": "off"}.get(str(mode).strip().lower(), str(mode).strip().lower())  # YAML reads on/off as booleans
    _override = mode if mode in ("auto", "on", "off") else "auto"


def season(day: date | None = None) -> bool:
    if _override != "auto":
        return _override == "on"
    day = day or date.today()
    return day.month == 10 and day.day >= SEASON_START


def night(day: date | None = None) -> bool:
    if _override != "auto":
        return _override == "on"
    day = day or date.today()
    return (day.month, day.day) == (10, 31)


# ── Rates: (chance per message, minimum minutes between, per channel) ──

def haunt_rate() -> tuple[float, int]:
    return (1 / 50, 45) if night() else (1 / 200, 180)


WANDER_RATE = (1 / 70, 120)    # the Night: a demon slips into a channel
GHOST_RATE = (1 / 90, 90)      # the Night: an old line flickers up
POSSESSION_CHANCE = 0.12       # the Night: a chat answer comes from another persona


# ── Trick or treat ──

_ASK = re.compile(r"^\W*trick\W+or\W+treat\W*$", re.IGNORECASE)
CANDY = (
    "Fun-size Copium Bar", "Candy Corn (suspicious)", "Toffee Apple, razor-free (probably)", "Ectoplasm Gummies",
    "Jawbreaker of the Old Ones", "Pumpkin Kek Cake", "Mog Mint", "Bag of Ratio Rocks", "Liquorice Lich",
)
TRICKS = ("spiders", "ghosted", "pumpkin")


def request(text: str) -> bool:
    return bool(_ASK.match(text.strip()))


def draw(rng: random.Random | None = None) -> tuple[str, str, int]:
    """("treat", candy, keks) or ("trick", kind, 0): treats a bit more often than tricks."""
    rng = rng or random.SystemRandom()
    if rng.random() < 0.6:
        return "treat", rng.choice(CANDY), rng.choice((0, 1, 2, 3, 5))
    return "trick", rng.choice(TRICKS), 0


TREAT_LINES = (
    "A hand that isn't quite a hand drops something into your bag.",
    "The door opens a crack. Something passes you a treat and closes it again, fast.",
    "You get a treat. Nobody was home.",
)
TRICK_LINES = {
    "spiders": "Wrong answer. Something with too many legs follows you home.",
    "ghosted": "You're being followed. Don't look behind you; it hates that.",
    "pumpkin": "The Pumpkin takes a kek. The Pumpkin always takes a kek.",
}


# ── The Night's dice ──

def witnessed(text: str, tones: list[str]) -> str:
    """A natural 1 or 20 on the Night has an audience."""
    if not night():
        return text
    if "crit" in tones:
        return f"{text}\n-# Somewhere under the floor, the dead applaud."
    if "fumble" in tones:
        return f"{text}\n-# Something in the walls laughed at that."
    return text
