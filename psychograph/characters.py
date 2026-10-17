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
    classic: str     # the D&D class it plays like, so people know what they're picking
    stat: str
    blurb: str
    move: str
    move_text: str


CLASSES: dict[str, Role] = {role.key: role for role in (
    Role("tapewarden", "Tapewarden", "Fighter", "mog", "Settles every dispute the old way: with a ruler and a witness.",
         "Tale of the Tape", "Measure a foe. Your next attack on it has advantage."),
    Role("brute", "Brute", "Barbarian", "mog", "Never invited, lifts anyway. Fuelled by every party they weren't told about.",
         "Rage of the Overlooked", "While nobody is paying you any attention, your MOG rolls have advantage."),
    Role("lurker", "Lurker", "Rogue", "mog", "Was in the channel the whole time. Never typed a word.",
         "Read Receipts", "Once a scene, act first and unseen: you were here all along."),
    Role("sophist", "Sophist", "Bard", "cope", "Has never lost an argument, by their own account.",
         "Cope Harder", "Once a scene, reroll a failed COPE check and keep the better."),
    Role("liturgist", "Liturgist", "Cleric", "aura", "Keeper of the Antiphon, where the words must be repeated exactly.",
         "Antiphony", "Once a scene, an ally who repeats your words exactly heals 1d6. A faithful typo heals 2d6."),
    Role("sigma", "Sigma", "Paladin", "aura", "Sworn to a code only they understand. The grindset is the oath.",
         "Lone Wolf", "When you act entirely alone, add +2 to the roll."),
    Role("clankwright", "Clankwright", "Wizard", "lore", "Studies the machines. Some of the machines study back.",
         "Machine Witness", "Once a session, ask the oracle twice and keep the answer you prefer."),
    Role("promptlock", "Promptlock", "Warlock", "lore", "Signed a pact with a nameless machine god. Never read the terms.",
         "Pact of the Context Window", "Once a session your patron answers anything. It always collects, later."),
    Role("spawnpilled", "Spawnpilled", "Sorcerer", "aura", "Born with it. Insufferable about it.",
         "Genetic Lottery", "Once a session, reroll any die. Fate was always on your side, or never was."),
    Role("grinder", "Grinder", "Monk", "mog", "An ascetic of the long grind. Level 60 Agility is a vow.",
         "Ironman", "Once a scene, doing something slow and tedious on purpose earns a Chuckle."),
    Role("grasstoucher", "Grasstoucher", "Ranger", "lore", "The only one who has been outside. Will not stop mentioning it.",
         "Touched Grass", "Advantage on checks made outdoors, far from any screen."),
)}

LEGACY_ROLES = {"arbiter": "sigma"}  # retired classes and factions, mapped to their nearest successor
LEGACY_FACTIONS = {"card": "jannie"}

# Classic checks, each read from one of the four scores, so "roll perception" works like it does at any table.
SKILLS = {
    "athletics": "mog", "acrobatics": "mog", "stealth": "mog", "sleight": "mog", "initiative": "mog", "intimidation": "mog",
    "deception": "cope", "performance": "cope", "willpower": "cope", "concentration": "cope",
    "persuasion": "aura", "insight": "aura", "animal": "aura", "charm": "aura",
    "perception": "lore", "investigation": "lore", "arcana": "lore", "history": "lore", "religion": "lore",
    "medicine": "lore", "nature": "lore", "survival": "lore",
}
SKILL_NAMES = {"sleight": "Sleight of Hand", "animal": "Animal Handling"}


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
    Faction("jannie", "The Unpaid Watch", "the Jannies",
            "Undying janitors who keep the roads clean and the wicked banned. They are not paid. They never were."),
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
    "a ban hammer, slightly chipped, origin unclear",
    "a frog-shaped seal that says nuh uh",
    "a bowl, for eating hotdogs out of on fight day",
)

BANDS = (  # (lowest total, key, label)
    (15, "yes", "Yes."),
    (10, "but", "Yes, but…"),
    (-99, "miss", "It goes wrong."),
)

_CHECK_WORDS = "|".join(sorted([*STATS, *SKILLS], key=len, reverse=True))
_STAT_ROLL = re.compile(
    r"^\s*(?P<pre>(?:save|saving throw)\s+)?(?P<word>" + _CHECK_WORDS + r")\b(?P<post>\s+(?:save|saving throw))?\s*"
    r"(?P<lean>adv|advantage|dis|disadvantage)?\s*(?:(?:for|#)\s*(?P<reason>.+))?$",
    re.IGNORECASE,
)


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
    location: str = "kekwick"     # where they are on the world map (world.py)
    notes: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.hp:
            self.hp = self.max_hp

    @property
    def max_hp(self) -> int:
        return 10 + modifier(self.scores.get("mog", 10))

    @property
    def role_info(self) -> Role:
        return CLASSES.get(LEGACY_ROLES.get(self.role, self.role), CLASSES["sophist"])

    @property
    def faction_info(self) -> Faction:
        return FACTIONS.get(LEGACY_FACTIONS.get(self.faction, self.faction), FACTIONS["none"])

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


def stat_request(text: str) -> tuple[str, str, str, str] | None:
    """(stat, "adv"|"dis"|"", reason, label) for "mog", "perception adv for traps", "save cope", "initiative"; else None."""
    match = _STAT_ROLL.match(text)
    if match is None:
        return None
    word = match["word"].lower()
    stat = word if word in STATS else SKILLS[word]
    if word in STATS:
        label = f"{STAT_NAMES[stat]} save" if match["pre"] or match["post"] else STAT_NAMES[stat]
    else:
        label = f"{SKILL_NAMES.get(word, word.title())} ({STAT_NAMES[stat]})"
    lean = (match["lean"] or "").lower()[:3]
    return stat, lean, (match["reason"] or "").strip(), label


@dataclass
class Check:
    stat: str
    faces: list[int]
    kept: int
    bonus: int
    reason: str
    label: str = ""

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


def check(character: Character, stat: str, lean: str = "", reason: str = "", label: str = "",
          rng: random.Random | None = None) -> Check:
    rng = rng or random.SystemRandom()
    faces = [rng.randint(1, 20) for _ in range(2 if lean in ("adv", "dis") else 1)]
    kept = max(faces) if lean == "adv" else min(faces) if lean == "dis" else faces[0]
    return Check(stat, faces, kept, character.mod(stat), reason, label or STAT_NAMES[stat])


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
        f"-# {character.name} ({who}) rolled {result.label}{reason} · {shown} {signed(result.bonus)}"
    )
