"""Spotting a request the bot has just answered, pasted again word for word or near enough.

A repeat within the window gets a one-line reply linking to that answer, instead of another model call. Text is compared after normalising (case, punctuation, spacing, mentions), and
near-copies count too (a 90% character match), so adding "pls" or a typo doesn't get round it.
Short messages are exempt: "hi" and "lol" are allowed to repeat.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher

REPEAT_WINDOW_SECONDS = 30 * 60
MIN_CHARS = 12
SIMILAR = 0.9
KEPT_PER_CHANNEL = 50


def normalise(text: str) -> str:
    text = re.sub(r"<[@#][!&]?\d+>|https?://\S+", " ", text.casefold())
    return " ".join(re.sub(r"[^\w\s]", " ", text).split())


@dataclass(frozen=True)
class Answered:
    text: str        # normalised request
    at: float        # when (unix seconds)
    link: str        # the answer's jump link

    @property
    def expires(self) -> float:
        return self.at + REPEAT_WINDOW_SECONDS


class RepeatGuard:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._answered: dict[int, list[Answered]] = {}

    def earlier(self, channel_id: int, text: str) -> Answered | None:
        """A recent answer to the same request in this channel, if there is one."""
        wanted = normalise(text)
        if len(wanted) < MIN_CHARS:
            return None
        now = self.clock()
        answered = [item for item in self._answered.get(channel_id, []) if item.expires > now]
        self._answered[channel_id] = answered
        for item in reversed(answered):
            if item.text == wanted:
                return item
            matcher = SequenceMatcher(None, item.text, wanted)
            if matcher.real_quick_ratio() >= SIMILAR and matcher.quick_ratio() >= SIMILAR and matcher.ratio() >= SIMILAR:
                return item
        return None

    def record(self, channel_id: int, text: str, link: str) -> None:
        wanted = normalise(text)
        if len(wanted) >= MIN_CHARS and link:
            answered = self._answered.setdefault(channel_id, [])
            answered.append(Answered(wanted, self.clock(), link))
            del answered[:-KEPT_PER_CHANNEL]
