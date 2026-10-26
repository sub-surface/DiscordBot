"""The Outside: #nanospasm's machinery (Land, the Ccru, Deleuze), with no language model anywhere.

  AQ           Anglossic Qabbala: digits at face value, A=10 … Z=35, summed. "aq: Nick Land"; and the channel's AQ
               index, which notices when two people's messages sum the same.
  TX           Tic-Xenotation (Barker, via Land's "Tic-Talk", Fanged Noumena p. 608): ':' is 2, '(n)' the nth prime,
               a number its prime factors in ascending order; Barker's deplex reaches 1 '(-P):' and 0 '((-P)):'.
  Pandemonium  the Ccru's 45 demons, one per pair of numogram zones (assets/numogram/pandemonium.json), drawn on the
               numogram with the demon's net-span lit.
  Quote bank   data/quotes.json, built by tools/build_quote_bank.py and kept out of git (the books are in copyright):
               "already said this" matching (BM25 over the bank) and fragments.
"""

from __future__ import annotations

import json
import math
import random
import re
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

from . import halloween

ROOT = Path(__file__).resolve().parent.parent
MATRIX_PATH = ROOT / "assets" / "numogram" / "pandemonium.json"
BANK_PATH = ROOT / "data" / "quotes.json"
WEEK_THEME_MIN = 30   # quotes a theme needs to be a week's theme: enough for 14 posts and the asking
FONTS = ROOT / "assets" / "fonts"

# ── AQ ──

def aq(text: str) -> int:
    """Anglossic Qabbala: 0-9 at face value, A=10 … Z=35; everything else (spaces, accents, emoji) counts nothing."""
    return sum(int(char, 36) for char in text.upper() if char.isascii() and char.isalnum())


def reduce(number: int) -> int:
    """Digital reduction, the numogram's way of folding a number into a zone: 333 → 9."""
    while number > 9:
        number = sum(map(int, str(number)))
    return number


# ── Tic-Xenotation ──

TX_LIMIT = 10**7  # a prime factor's index comes from one sieve this size (10 MB, built once): don't raise it lightly


def _factors(number: int) -> list[int]:
    out, divisor = [], 2
    while divisor * divisor <= number:
        while number % divisor == 0:
            out.append(divisor)
            number //= divisor
        divisor += 1 if divisor == 2 else 2
    return out + ([number] if number > 1 else [])


@lru_cache(maxsize=1)
def _sieve() -> bytearray:
    """1 at every prime below TX_LIMIT: the only allocation TX makes, whatever it's asked."""
    sieve = bytearray([1]) * TX_LIMIT
    sieve[0:2] = b"\x00\x00"
    for value in range(2, int(TX_LIMIT ** 0.5) + 1):
        if sieve[value]:
            sieve[value * value::value] = bytearray(len(range(value * value, TX_LIMIT, value)))
    return sieve


def _prime_index(prime: int) -> int:
    """Which prime this is (2 is the 1st): only ever called on factors of numbers below TX_LIMIT."""
    return 1 if prime == 2 else sum(_sieve()[:prime + 1])


def tx(number: int) -> str:
    """`number` in Tic-Xenotation. ValueError for negatives and for numbers too big to factor at once."""
    if number < 0 or number >= TX_LIMIT:
        raise ValueError("TX counts from 0 up to ten million here")
    if number == 0:
        return "((-P)):"
    if number == 1:
        return "(-P):"
    return "".join(":" if prime == 2 else f"({tx(_prime_index(prime))})" for prime in _factors(number))


# ── Pandemonium ──

@dataclass(frozen=True)
class Demon:
    span: str        # net-span, "5::4"
    name: str
    kind: str        # "Syzygetic Chronodemon", "Amphidemon", …
    power: str       # its epithet in the matrix, "Cataclysmic Convergence"

    @property
    def zones(self) -> tuple[int, int]:
        high, low = self.span.split("::")
        return int(high), int(low)

    @property
    def syzygy(self) -> bool:
        return sum(self.zones) == 9


@lru_cache(maxsize=1)
def demons() -> tuple[Demon, ...]:
    return tuple(Demon(*row) for row in json.loads(MATRIX_PATH.read_text(encoding="utf-8"))["demons"])


def summon(text: str = "", rng: random.Random | None = None) -> Demon:
    """A demon: by the AQ of what was said ("summon: Hugh Chungus" always brings the same one), else at random."""
    if text.strip():
        return demons()[aq(text) % len(demons())]
    if halloween.night():  # Halloween: only the Outside answers an open call
        return (rng or random.SystemRandom()).choice([demon for demon in demons() if demon.kind.endswith("Xenodemon")])
    return (rng or random.SystemRandom()).choice(demons())


def gate(zone: int) -> int:
    """A zone's gate: its digital cumulation (1+2+…+n), reduced. Zone 7 cumulates to 28, so its gate is Gt-28 → 1."""
    return reduce(zone * (zone + 1) // 2)


CURRENTS = {(5, 4): 1, (7, 2): 5, (8, 1): 7, (6, 3): 3, (9, 0): 9}  # each syzygy's difference: the primary flows
KIND_ACCENTS = {"Chronodemon": 0xC8A15A, "Amphidemon": 0x9E2A2B, "Xenodemon": 0x6A4C93}  # brass, oxblood, violet


def accent(demon: Demon) -> int:
    if halloween.night():
        return halloween.PUMPKIN  # the numogram burns orange on Halloween
    return next((colour for kind, colour in KIND_ACCENTS.items() if demon.kind.endswith(kind)), 0xC8A15A)


# The numogram, stylised: the Warp (6::3) above, the Time-Circuit's three syzygies, the Plex (9::0) below.
_ZONES = {6: (0, 0), 3: (1, 0), 5: (0, 1), 4: (1, 1), 7: (0, 2), 2: (1, 2), 8: (0, 3), 1: (1, 3), 9: (0, 4), 0: (1, 4)}
_REGIONS = ("WARP", "TIME-CIRCUIT", "", "", "PLEX")


def numogram(demon: Demon | None = None, width: int = 520):
    """The numogram as an image, with `demon`'s two zones and its net-span lit (a syzygy's own line, if it's one)."""
    from PIL import Image, ImageDraw, ImageFont

    scale, height = 3, int(width * 1.5)
    w, h = width * scale, height * scale
    charcoal, bone, faint, brass = (20, 22, 27), (236, 230, 218), (88, 88, 88), (200, 161, 90)  # faint: bone at ~30%, pre-blended
    lit = tuple(int(f"{accent(demon):06x}"[i:i + 2], 16) for i in (0, 2, 4)) if demon else brass
    image = Image.new("RGB", (w, h), charcoal)
    draw = ImageDraw.Draw(image)

    def font(name: str, size: float):
        try:
            return ImageFont.truetype(str(FONTS / name), int(size * scale))
        except OSError:
            return ImageFont.load_default(size=int(size * scale))

    top, row = h * 0.09, h * 0.165
    left, right, radius = w * 0.3, w * 0.7, w * 0.075
    at = {zone: (left if column == 0 else right, top + r * row + (row * 0.25 if r else 0)) for zone, (column, r) in _ZONES.items()}
    for r, label in enumerate(_REGIONS):
        if label:
            y = top + r * row + (row * 0.25 if r else 0)
            draw.text((w * 0.5, y - radius * 1.55), label, fill=faint, font=font("BarlowSemiCondensed-Medium.ttf", 11), anchor="mm")
    for (high, low), current in CURRENTS.items():
        (x1, y1), (x2, y2) = at[high], at[low]
        span_lit = demon is not None and demon.syzygy and set(demon.zones) == {high, low}
        draw.line((x1 + radius, y1, x2 - radius, y2), fill=lit if span_lit else faint, width=int((5 if span_lit else 2) * scale))
        draw.text(((x1 + x2) / 2, y1 - 9 * scale), str(current), fill=faint, font=font("BarlowSemiCondensed-Medium.ttf", 12), anchor="mm")
    if demon is not None and not demon.syzygy:
        (x1, y1), (x2, y2) = (at[zone] for zone in demon.zones)
        if x1 == x2:  # one column: bow outward, past the edge of the diagram
            cx, cy = x1 + (-1 if x1 < w / 2 else 1) * (abs(y2 - y1) * 0.3 + radius * 1.6), (y1 + y2) / 2
        else:  # across: bow sideways off the straight line, a quarter of its length
            length = math.dist((x1, y1), (x2, y2))
            nx, ny = -(y2 - y1) / length, (x2 - x1) / length
            cx, cy = (x1 + x2) / 2 + nx * length * 0.25, (y1 + y2) / 2 + ny * length * 0.25
        points = [((1 - t) ** 2 * x1 + 2 * (1 - t) * t * cx + t * t * x2,
                   (1 - t) ** 2 * y1 + 2 * (1 - t) * t * cy + t * t * y2) for t in (i / 80 for i in range(81))]
        points = [p for p in points if math.dist(p, (x1, y1)) > radius and math.dist(p, (x2, y2)) > radius]
        draw.line(points, fill=lit, width=int(4 * scale), joint="curve")
    numeral = font("BarlowSemiCondensed-Medium.ttf", 30)  # Marcellus's 0 reads as a letter O
    for zone, (x, y) in at.items():
        on = demon is not None and zone in demon.zones
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=lit if on else charcoal,
                     outline=lit if on else bone, width=int(2.5 * scale))
        draw.text((x, y + 1 * scale), str(zone), fill=charcoal if on else bone, font=numeral, anchor="mm")
    if demon is not None:
        span, name = font("BarlowSemiCondensed-Medium.ttf", 26), font("Marcellus-Regular.ttf", 26)
        gap = 12 * scale
        left_edge = w * 0.5 - (draw.textlength(demon.span, font=span) + gap + draw.textlength(demon.name, font=name)) / 2
        draw.text((left_edge, h * 0.93), demon.span, fill=lit, font=span, anchor="lm")
        draw.text((left_edge + draw.textlength(demon.span, font=span) + gap, h * 0.93), demon.name, fill=lit, font=name, anchor="lm")
    return image.resize((width, height), Image.LANCZOS)


# ── The quote bank ──

STOP = frozenset("""a about above after again against all also am an and any are as at be because been before being below
between both but by can could did do does doing down during each few for from further had has have having he her here hers
herself him himself his how i if in into is it its itself just me more most my myself no nor not now of off on once only or
other our ours ourselves out over own same she should so some such than that the their theirs them themselves then there
these they this those through to too under until up very was we were what when where which while who whom why will with
would you your yours yourself yourselves one ever even never yet thus itself lol lmao like get got just really think thing
things people make made makes know something anything everything nothing else longer sense way ways much many still going
want need good bad well also back down here right sure pretty quite rather said says say saying being already lot actually
mean means probably kind sort whole part""".split())


def terms(text: str) -> list[str]:
    """Words for matching: lowercase, no stopwords, cut to six letters so 'machine' meets 'machinic'."""
    return [word[:6] for word in re.findall(r"[a-z]{3,}", text.lower()) if word not in STOP]


@dataclass
class Quote:
    id: str
    text: str
    paragraph: str
    author: str
    title: str
    year: int
    page: int | None
    theme: str = ""

    @property
    def source(self) -> str:
        page = f" · p. {self.page}" if self.page else ""
        year = f"c. {-self.year} BC" if self.year < 0 else self.year
        return f"{self.author} · *{self.title}* ({year}){page}"


class Bank:
    """The quotes and a BM25 index over them. Empty (and every lookup None) when data/quotes.json isn't built."""

    K1, B = 1.4, 0.6
    MIN_SHARED = 3      # distinct rare words a message must share with a quote to count as "already said"
    MIN_SCORE = 10.0    # with a two-word phrase in common ("body without organs")…
    SURE_SCORE = 17.0   # …or this much without one

    def __init__(self, quotes: list[Quote]) -> None:
        self.quotes = quotes
        self.by_id = {quote.id: quote for quote in quotes}
        self.docs = [terms(quote.text) for quote in quotes]
        self.avg = sum(map(len, self.docs)) / len(self.docs) if self.docs else 0
        counts: dict[str, int] = {}
        for doc in self.docs:
            for term in set(doc):
                counts[term] = counts.get(term, 0) + 1
        n = len(self.docs)
        self.idf = {term: math.log(1 + (n - c + 0.5) / (c + 0.5)) for term, c in counts.items()}

    @classmethod
    def load(cls, path: Path = BANK_PATH) -> Bank:
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))["quotes"]
        except (OSError, ValueError, KeyError):
            return cls([])
        return cls([Quote(**{key: row.get(key) for key in Quote.__dataclass_fields__ if key in row}) for row in rows])

    def match(self, text: str) -> tuple[Quote, float] | None:
        """The quote that most nearly says what `text` says, if one shares enough rare words with it."""
        said = terms(text)
        query = set(said) & self.idf.keys()
        if len(query) < self.MIN_SHARED:
            return None
        pairs = set(zip(said, said[1:]))
        best: tuple[float, int] | None = None
        for index, doc in enumerate(self.docs):
            shared = query.intersection(doc)
            if len(shared) < self.MIN_SHARED:
                continue
            score = sum(self.idf[t] * doc.count(t) * (self.K1 + 1)
                        / (doc.count(t) + self.K1 * (1 - self.B + self.B * len(doc) / self.avg)) for t in shared)
            phrase = bool(pairs.intersection(zip(doc, doc[1:])))
            if score >= (self.MIN_SCORE if phrase else self.SURE_SCORE) and (best is None or score > best[0]):
                best = (score, index)
        return (self.quotes[best[1]], best[0]) if best else None

    def random(self, rng: random.Random | None = None, author: str | None = None) -> Quote | None:
        pool = [quote for quote in self.quotes if author is None or quote.author == author]
        return (rng or random.SystemRandom()).choice(pool) if pool else None

    @property
    def authors(self) -> list[str]:
        """Each author once, the most quoted first."""
        counts: dict[str, int] = {}
        for quote in self.quotes:
            counts[quote.author] = counts.get(quote.author, 0) + 1
        return sorted(counts, key=lambda author: (-counts[author], author))

    def author_named(self, text: str) -> str | None:
        """The author `text` names: "hegel", "Deleuze", "nick land" (whole name or any part of it)."""
        wanted = text.casefold().strip()
        if not wanted:
            return None
        for author in self.authors:
            if wanted == author.casefold() or wanted in author.casefold().split():
                return author
        return None

    def search(self, text: str, author: str | None = None, rng: random.Random | None = None, top: int = 5) -> Quote | None:
        """A quote about `text`: one of the `top` best BM25 matches (so asking twice can bring another), within
        one author's when given. None when nothing shares a word with it. Looser than match(), which has to be sure."""
        query = set(terms(text)) & self.idf.keys()
        if not query:
            return None
        scored = []
        for index, doc in enumerate(self.docs):
            if author is not None and self.quotes[index].author != author:
                continue
            if shared := query.intersection(doc):
                score = sum(self.idf[t] * doc.count(t) * (self.K1 + 1)
                            / (doc.count(t) + self.K1 * (1 - self.B + self.B * len(doc) / self.avg)) for t in shared)
                scored.append((score, index))
        if not scored:
            return None
        best = sorted(scored, reverse=True)[:top]
        return self.quotes[(rng or random.SystemRandom()).choice(best)[1]]

    @staticmethod
    def theme_of(quote: Quote) -> str:
        """A curator's theme, folded so "The State" and "state" are one: lowercase, no leading article."""
        return re.sub(r"^(?:the|a|an)\s+", "", quote.theme.lower().strip())

    def themes(self, minimum: int = WEEK_THEME_MIN) -> dict[str, list[Quote]]:
        """Themes with at least `minimum` quotes, each with its quotes."""
        grouped: dict[str, list[Quote]] = {}
        for quote in self.quotes:
            if theme := self.theme_of(quote):
                grouped.setdefault(theme, []).append(quote)
        return {theme: quotes for theme, quotes in grouped.items() if len(quotes) >= minimum}

    def week_theme(self, day: date) -> str | None:
        """This week's theme: the same all week (and across restarts), chosen by the ISO week from the big themes."""
        themes = sorted(self.themes())
        if not themes:
            return None
        year, week, _ = day.isocalendar()
        return random.Random(f"theme-{year}-{week}").choice(themes)

    def fragment(self, about: str = "", author: str | None = None, rng: random.Random | None = None) -> Quote | None:
        """What a request for a fragment brings: on `about` if given (falling back to chance), from `author` if given."""
        return (self.search(about, author, rng) if about else None) or self.random(rng, author)


# ── Asking for things in chat ──

_AQ_ASK = re.compile(r"^\s*aq\s*[:=]?\s+(?P<text>.+)$", re.IGNORECASE | re.DOTALL)
_TX_ASK = re.compile(r"^\s*tx\s*[:=]?\s*(?P<number>[\d,_ ]{1,20})\s*$", re.IGNORECASE)
_SUMMON = re.compile(r"^\s*(?:summon(?:\s+(?:a|me\s+a)\s+demon)?|draw\s+(?:a|me\s+a)\s+demon)(?=\W|$)(?P<rest>.*)$",
                     re.IGNORECASE | re.DOTALL)
_FRAGMENT = re.compile(r"^\s*(?:a\s+)?fragment(?:\s*[:—–-]\s*(?P<about>.+))?\s*$", re.IGNORECASE | re.DOTALL)
_LEAD = re.compile(r"^\s*(?:[:,;—–-]|for\b)\s*", re.IGNORECASE)


def request(text: str) -> tuple[str, str] | None:
    """("aq", text), ("tx", number), ("summon", text) or ("fragment", about) for a message asking for one, else None.
    "fragment" alone or "fragment: time" / "fragment: hegel" (an author's name brings one of theirs)."""
    text = text.strip()
    if len(text) > 400:
        return None
    if match := _TX_ASK.match(text):
        return "tx", re.sub(r"[,_ ]", "", match["number"])
    if match := _AQ_ASK.match(text):
        return "aq", match["text"].strip()
    if match := _FRAGMENT.match(text):
        return "fragment", match["about"] or ""
    if match := _SUMMON.match(text):
        rest = match["rest"].strip()
        lead = _LEAD.match(rest)
        if rest and lead is None:
            return None  # "summon the council": chat, not a summoning; "summon: the council" is one
        return "summon", rest[lead.end():].strip() if lead else ""
    return None
