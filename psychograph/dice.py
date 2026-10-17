"""Dice: tabletop notation, rolled locally. No model, no Jev, no GPU.

  d20, 2d6+3, 1d8+1d6+2     sums of dice and modifiers
  4d6kh3, 2d20kl1           keep the highest / lowest N (dh / dl drop them instead)
  d6!                       exploding: a die showing its maximum rolls again and adds
  d%                        a d100
  adv / dis                 a d20 rolled twice, keeping the higher / lower (with any modifier: adv+5)
  6x 4d6kh3                 the same roll several times (a stat block)
  … for stealth / # sneak   the rest is the reason, shown with the result

Reached by `/roll`, by mentioning the bot ("@bot roll for initiative": no dice given means a d20),
or by a message that is only a roll ("roll 2d6+1"), matched strictly so chat never sets it off.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

MAX_DICE = 100        # per term
MAX_SIDES = 1000
MAX_SETS = 12
MAX_EXPLOSIONS = 50   # per term, so a d1! can't spin forever

_TERM = re.compile(r"([+-])?\s*(?:(\d*)d(\d+|%)(?:(kh|kl|dh|dl)(\d+))?(!)?|(\d+))", re.IGNORECASE)
_SETS = re.compile(r"^\s*(\d+)\s*x\s+|\s+x\s*(\d+)\s*$", re.IGNORECASE)
_REASON = re.compile(r"\s+(?:for|#)\s*(.+)$|\s*#\s*(.+)$", re.IGNORECASE)
_SHORTHAND = {"adv": "2d20kh1", "advantage": "2d20kh1", "dis": "2d20kl1", "disadvantage": "2d20kl1"}
_ROLL_WORD = re.compile(r"^\s*(?:roll|r)\b\s*", re.IGNORECASE)


class DiceError(ValueError):
    """Notation that can't be rolled, worded for the person who typed it."""


@dataclass
class Term:
    sign: int
    count: int = 0            # 0 for a flat modifier
    sides: int = 0
    keep: str | None = None   # kh, kl, dh or dl
    keep_n: int = 0
    explode: bool = False
    flat: int = 0

    def notation(self) -> str:
        if not self.count:
            return str(self.flat)
        keep = f"{self.keep}{self.keep_n}" if self.keep else ""
        return f"{self.count if self.count > 1 else ''}d{self.sides}{keep}{'!' if self.explode else ''}"


@dataclass
class Rolled:
    term: Term
    faces: list[int] = field(default_factory=list)
    kept: list[bool] = field(default_factory=list)

    @property
    def value(self) -> int:
        if not self.term.count:
            return self.term.sign * self.term.flat
        return self.term.sign * sum(face for face, keep in zip(self.faces, self.kept) if keep)

    def shown(self) -> str:
        if not self.term.count:
            return str(self.term.flat)
        return "[" + ", ".join(str(face) if keep else f"~~{face}~~" for face, keep in zip(self.faces, self.kept)) + "]"


@dataclass
class Roll:
    notation: str
    reason: str
    sets: list[list[Rolled]]

    @property
    def totals(self) -> list[int]:
        return [sum(part.value for part in parts) for parts in self.sets]

    @property
    def biggest_die(self) -> int:
        return max((part.term.sides for parts in self.sets for part in parts if part.term.count), default=20)

    def natural(self) -> int | None:
        """The kept face of a lone d20 roll (d20, adv, dis, d20+5), for nat 20 / nat 1 flair."""
        if len(self.sets) != 1:
            return None
        dice = [part for part in self.sets[0] if part.term.count]
        if len(dice) != 1 or dice[0].term.sides != 20 or dice[0].term.explode:
            return None
        kept = [face for face, keep in zip(dice[0].faces, dice[0].kept) if keep]
        return kept[0] if len(kept) == 1 else None


def parse(text: str) -> tuple[list[Term], int, str]:
    """(terms, how many sets, reason). Raises DiceError on anything it can't roll."""
    text = text.strip()
    reason = ""
    if match := _REASON.search(text):
        reason = (match.group(1) or match.group(2) or "").strip()
        text = text[: match.start()].strip()
    sets = 1
    if match := _SETS.search(text):
        sets = int(match.group(1) or match.group(2))
        text = (text[: match.start()] + text[match.end():]).strip()
    if not 1 <= sets <= MAX_SETS:
        raise DiceError(f"Up to {MAX_SETS} sets at once.")
    lowered = text.lower()
    for word, notation in _SHORTHAND.items():
        if lowered == word or lowered.startswith((word + "+", word + "-", word + " ")):
            text = notation + text[len(word):]
            break
    compact = re.sub(r"\s+", "", text)
    if not compact:
        raise DiceError("Give me some dice: `d20`, `2d6+3`, `4d6kh3`.")
    terms, position = [], 0
    for match in _TERM.finditer(compact):
        if match.start() != position or (terms and not match.group(1)):
            break
        sign = -1 if match.group(1) == "-" else 1
        if match.group(7):
            terms.append(Term(sign, flat=int(match.group(7))))
        else:
            count = int(match.group(2) or 1)
            sides = 100 if match.group(3) == "%" else int(match.group(3))
            keep, keep_n = (match.group(4) or "").lower() or None, int(match.group(5) or 0)
            if not 1 <= count <= MAX_DICE or not 1 <= sides <= MAX_SIDES:
                raise DiceError(f"Up to {MAX_DICE} dice of up to {MAX_SIDES} sides.")
            if keep and not 1 <= keep_n <= count:
                raise DiceError(f"Can't keep or drop {keep_n} of {count} dice.")
            terms.append(Term(sign, count, sides, keep, keep_n, bool(match.group(6))))
        position = match.end()
    if position != len(compact) or not any(term.count for term in terms):
        raise DiceError(f"I can't read `{text}` as dice. Try `d20`, `2d6+3`, `4d6kh3` or `adv+5`.")
    return terms, sets, reason


def _roll_term(term: Term, rng: random.Random) -> Rolled:
    if not term.count:
        return Rolled(term)
    faces = []
    for _ in range(term.count):
        face = rng.randint(1, term.sides)
        faces.append(face)
        explosions = 0
        while term.explode and term.sides > 1 and face == term.sides and explosions < MAX_EXPLOSIONS:
            face = rng.randint(1, term.sides)
            faces.append(face)
            explosions += 1
    kept = [True] * len(faces)
    if term.keep:
        # Sorted so the dice to keep (kh, kl) or to drop (dh, dl) come first.
        order = sorted(range(len(faces)), key=lambda index: faces[index], reverse=term.keep in ("kh", "dh"))
        chosen = set(order[: term.keep_n]) if term.keep in ("kh", "kl") else set(order[term.keep_n:])
        kept = [index in chosen for index in range(len(faces))]
    return Rolled(term, faces, kept)


def roll(text: str, rng: random.Random | None = None) -> Roll:
    terms, sets, reason = parse(text)
    rng = rng or random.SystemRandom()
    notation = "".join(
        ("" if index == 0 and term.sign > 0 else ("-" if term.sign < 0 else "+")) + term.notation()
        for index, term in enumerate(terms)
    )
    if sets > 1:
        notation = f"{sets}× {notation}"
    return Roll(notation, reason, [[_roll_term(term, rng) for term in terms] for _ in range(sets)])


def request(prompt: str, strict: bool = False) -> str | None:
    """The dice part of a message asking for a roll, or None.

    strict (a message nobody addressed to the bot): the whole message must be "roll <dice>[ for …]".
    Otherwise (a mention): it starts with "roll", and with no dice given it's a d20 ("roll for initiative").
    """
    match = _ROLL_WORD.match(prompt)
    if match is None:
        return None
    rest = prompt[match.end():].strip()
    from .characters import stat_request  # imported here: characters imports dice

    if stat_request(rest) is not None:
        return rest  # a check with a character's score or skill ("roll perception"): characters.py
    try:
        parse(rest)
        return rest
    except DiceError:
        pass
    if re.match(r"^(?:for\s+\S|#)", rest, re.IGNORECASE) or (not rest and not strict):
        return f"d20 {rest}".strip()  # "roll for initiative": a d20
    return None if strict else rest


_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+|\n+")


def embedded_request(text: str) -> str | None:
    """A roll written into a roleplay message ("I pick the lock. Roll lore"): the first line or sentence that is
    on its own a strict roll. Anything that merely mentions rolling ("I'll roll with it") doesn't count."""
    for piece in _SENTENCE_END.split(text.strip()):
        notation = request(piece.strip().rstrip(".!"), strict=True)
        if notation is not None:
            return notation
    return None


def die_icon_name(sides: int) -> str:
    """Which die icon fits: the standard die at or above the biggest one rolled."""
    for standard in (4, 6, 8, 10, 12, 20):
        if sides <= standard:
            return f"die_d{standard}"
    return "die_d20"


def tone(result: Roll) -> str:
    natural = result.natural()
    return "crit" if natural == 20 else "fumble" if natural == 1 else "normal"


def render(result: Roll, who: str, icon: str = "") -> str:
    """Plain text: the total up front, the faces in subtext. Nat 20 / nat 1 get a word of their own.
    `icon` leads the line when there's no result image to show the die."""
    reason = f" for **{result.reason}**" if result.reason else ""
    headline = " · ".join(f"**{total}**" for total in result.totals)
    flair = {"crit": " — *natural 20!*", "fumble": " — *natural 1…*"}.get(tone(result), "")
    lines = [f"{icon + ' ' if icon else ''}{headline}{flair}", f"-# {who} rolled `{result.notation}`{reason}"]
    for parts in result.sets:
        shown = " ".join(
            ("" if index == 0 and part.term.sign > 0 else ("- " if part.term.sign < 0 else "+ ")) + part.shown()
            for index, part in enumerate(parts)
        )
        lines.append(f"-# {shown}")
    text = "\n".join(lines)
    return text if len(text) <= 1900 else "\n".join(lines[:2]) + "\n-# (too many dice to list)"
