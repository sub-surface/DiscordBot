"""The Scrollands: characters and the one rule (d20 + a score). No model, no GPU.

Four scores: MOG (bodies, force, anything measurable), COPE (argument, nerve, talking your way out), AURA
(presence, charm) and LORE (knowing things, reading the PDFs). A check is d20 + the score's modifier:

  1-9 it goes wrong · 10-14 yes, but · 15+ yes · a natural 20 is mogged · a natural 1 is spawnpilled

A character's scores are their spawnpill: 4d6kh3 four times, or 15/13/11/9, placed by the player. Everything
else here is data the sheet and the creation form read: classes, factions and keepsakes, written so the world
reads as fantasy first and the server's in-jokes sit underneath it.
"""

from __future__ import annotations

import random
import re
from dataclasses import asdict, dataclass, field

from . import dice

STATS = ("mog", "cope", "aura", "lore")
STAT_NAMES = {"mog": "MOG", "cope": "COPE", "aura": "AURA", "lore": "LORE"}
STAT_HINTS = {
    "mog": "bodies, force, anything measurable",
    "cope": "argument, nerve, talking your way out",
    "aura": "presence, charm, the vibe",
    "lore": "knowing things, reading the PDFs",
}
STANDARD_SET = (15, 13, 11, 9)
STARTING_KEKS = 100
MAX_CHUCKLES = 3


@dataclass(frozen=True)
class Role:
    key: str
    name: str
    stat: str
    blurb: str
    move: str
    move_text: str


CLASSES: dict[str, Role] = {role.key: role for role in (
    Role("tapewarden", "Tapewarden", "mog", "Settles disputes the old way: by measuring them.",
         "Tale of the Tape", "Measure a foe. Your next MOG roll against it has advantage."),
    Role("sophist", "Sophist", "cope", "Has never lost an argument, by their own account.",
         "I Recognize the Distinction", "Once a scene, reroll a failed COPE roll and keep the better."),
    Role("arbiter", "Arbiter", "cope", "Carries the cards. Just, and forgiving.",
         "Yellow Card", "Book anyone who says a thing and its opposite. Their next roll has disadvantage."),
    Role("liturgist", "Liturgist", "aura", "Keeper of the Antiphon, where the words matter exactly.",
         "Antiphony", "Once a scene, an ally who repeats your words exactly heals 1d6. A faithful typo heals 2d6."),
    Role("clankwright", "Clankwright", "lore", "Builds, befriends and impeaches machines.",
         "Machine Witness", "Once a session, ask the oracle twice and keep the answer you prefer."),
    Role("grinder", "Grinder", "mog", "An ascetic of the long grind. Level 60 Agility is a vow.",
         "Ironman", "Once a scene, doing something slow and tedious on purpose earns a Chuckle."),
)}


@dataclass(frozen=True)
class Faction:
    key: str
    name: str
    nickname: str
    blurb: str


FACTIONS: dict[str, Faction] = {faction.key: faction for faction in (
    Faction("ascent", "The Court of Ascent", "the Hypergamous Court",
            "Old nobility that only ever marries up, and keeps a ledger of who answers whom."),
    Faction("iron", "The Iron Brotherhood", "the Blackpill Brothers",
            "Ascetic lifters who hold that fate is fixed at spawn, and train anyway."),
    Faction("card", "The Order of the Yellow Card", "the Referees",
            "Magistrates who rule in six words or fewer, sealed with a frog that says nuh uh."),
    Faction("clank", "The Clankworks", "the Clankers",
            "Machine folk and their keepers: Omnius, Bernard the record-keeper, Mini Me the seer."),
    Faction("exchange", "The Exchange", "the Metric",
            "A trading floor where reactions are labour, and the Metric is never wrong."),
    Faction("none", "Unaligned", "a free agent", "Owes nobody, and nobody owes them."),
)}

KEEPSAKES = (
    "a plastic ruler, exactly 8.2 cm of it used",
    "an uncharged amulet of glory",
    "a teal duffel bag that answers to Lamont",
    "a fifty-two piece cookware set (lids counted)",
    "a car key with no car",
    "a slice of pizza that cost one and a half keks and never goes stale",
    "a poster of a famous knife fight",
    "a block list with one name you can't remember adding",
    "an unread PDF you carry everywhere",
    "a yellow card, already used once",
    "a frog-shaped seal that says nuh uh",
    "a bowl, for eating hotdogs out of on fight day",
)

BANDS = (  # (lowest total, key, label)
    (15, "yes", "Yes."),
    (10, "but", "Yes, but…"),
    (-99, "miss", "It goes wrong."),
)

_STAT_ROLL = re.compile(r"^\s*(mog|cope|aura|lore)\b\s*(adv|advantage|dis|disadvantage)?\s*(?:(?:for|#)\s*(.+))?$", re.IGNORECASE)


def modifier(score: int) -> int:
    return (score - 10) // 2


def signed(number: int) -> str:
    return f"+{number}" if number >= 0 else f"−{-number}"


@dataclass
class Character:
    name: str
    role: str
    faction: str
    scores: dict[str, int]
    keepsake: str = ""
    look: str = ""
    pronouns: str = ""
    level: int = 1
    hp: int = 0
    keks: int = STARTING_KEKS
    chuckles: int = 1
    portrait: str = ""            # an image URL (the member's avatar unless they choose one)
    notes: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.hp:
            self.hp = self.max_hp

    @property
    def max_hp(self) -> int:
        return 10 + modifier(self.scores.get("mog", 10))

    @property
    def role_info(self) -> Role:
        return CLASSES.get(self.role, CLASSES["sophist"])

    @property
    def faction_info(self) -> Faction:
        return FACTIONS.get(self.faction, FACTIONS["none"])

    def mod(self, stat: str) -> int:
        return modifier(self.scores.get(stat, 10))

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> Character:
        known = {name for name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})


def spawnpill(rng: random.Random | None = None) -> list[int]:
    """Four scores, each 4d6 keeping the highest three, highest first."""
    result = dice.roll("4x 4d6kh3", rng)
    return sorted(result.totals, reverse=True)


def stat_request(text: str) -> tuple[str, str, str] | None:
    """("mog", "adv"|"dis"|"", reason) for "mog", "cope adv for the door", "lore # history"; else None."""
    match = _STAT_ROLL.match(text)
    if match is None:
        return None
    lean = (match.group(2) or "").lower()[:3]
    return match.group(1).lower(), lean, (match.group(3) or "").strip()


@dataclass
class Check:
    stat: str
    faces: list[int]
    kept: int
    bonus: int
    reason: str

    @property
    def total(self) -> int:
        return self.kept + self.bonus

    @property
    def natural(self) -> int:
        return self.kept

    @property
    def band(self) -> tuple[str, str]:
        if self.natural == 20:
            return "crit", "Mogged."
        if self.natural == 1:
            return "fumble", "Spawnpilled."
        return next((key, label) for lowest, key, label in BANDS if self.total >= lowest)


def check(character: Character, stat: str, lean: str = "", reason: str = "", rng: random.Random | None = None) -> Check:
    rng = rng or random.SystemRandom()
    faces = [rng.randint(1, 20) for _ in range(2 if lean in ("adv", "dis") else 1)]
    kept = max(faces) if lean == "adv" else min(faces) if lean == "dis" else faces[0]
    return Check(stat, faces, kept, character.mod(stat), reason)


def render_check(result: Check, character: Character, who: str) -> str:
    """The check in plain text: the band as the headline, the arithmetic in subtext."""
    key, label = result.band
    reason = f" for **{result.reason}**" if result.reason else ""
    faces = result.faces
    shown = f"[{faces[0]}]" if len(faces) == 1 else "[" + ", ".join(
        str(face) if face == result.kept and index == faces.index(result.kept) else f"~~{face}~~"
        for index, face in enumerate(faces)) + "]"
    return (
        f"**{result.total}** · {label}\n"
        f"-# {character.name} ({who}) rolled {STAT_NAMES[result.stat]}{reason} · {shown} {signed(result.bonus)}"
    )
