"""Combat, the Baldur's Gate way: an attack roll against a defence, then damage dice if it lands.

  attack = d20 + the weapon's score modifier + proficiency (+2, rising every four levels)
  defence = 10 + armour + MOG modifier
  It hits when the attack meets the defence. A natural 20 always hits and doubles the damage dice; a natural 1
  always misses. Damage = the weapon's dice + the same modifier (at least 1).

Each class fights with a weapon that fits it. Context is the DM's: advantage, disadvantage, and bonus dice for a
Lurker striking unseen or a Brute who wasn't invited. Monsters attack with a flat bonus and damage notation
("+4, 1d8+2"), so the DM can stat anything in one line.
"""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from .characters import Character, signed


@dataclass(frozen=True)
class Weapon:
    name: str
    dice: str     # "1d8"
    stat: str


WEAPONS = {
    "tapewarden": Weapon("the measuring rod", "1d10", "mog"),
    "brute": Weapon("a folding chair", "1d12", "mog"),
    "lurker": Weapon("a shiv from the cellar", "1d6", "mog"),
    "sophist": Weapon("a cutting remark", "1d6", "cope"),
    "liturgist": Weapon("the swung censer", "1d6", "aura"),
    "sigma": Weapon("an oathblade, grindset etched", "1d8", "aura"),
    "clankwright": Weapon("an arc bolt", "1d10", "lore"),
    "promptlock": Weapon("an eldritch prompt", "1d10", "lore"),
    "spawnpilled": Weapon("a genetic flare", "1d8", "aura"),
    "grinder": Weapon("bare knuckles, sixty levels deep", "1d8", "mog"),
    "grasstoucher": Weapon("a longbow, actually used outdoors", "1d8", "lore"),
}
ARMOUR = {  # heavy for the oath-bound and the duellists, nothing for those who never leave the tower
    "tapewarden": 4, "sigma": 4, "liturgist": 3, "brute": 2, "grinder": 2, "grasstoucher": 2,
    "lurker": 1, "sophist": 1, "promptlock": 1, "spawnpilled": 0, "clankwright": 0,
}
DEFAULT_WEAPON = Weapon("a sharpened opinion", "1d6", "mog")

_TERM = re.compile(r"([+-]?)\s*(?:(\d*)d(\d+)|(\d+))", re.IGNORECASE)
_REQUEST = re.compile(
    r"^\s*(?P<what>attack|damage|dmg)\b\s*(?P<lean>adv|advantage|dis|disadvantage)?\s*"
    r"(?:(?:vs\.?|against|ac)\s*(?P<ac>\d{1,2}))?\s*(?P<lean2>adv|advantage|dis|disadvantage)?\s*(?:(?:for|#)\s*(?P<reason>.+))?$",
    re.IGNORECASE,
)


class CombatError(ValueError):
    pass


def weapon(character: Character) -> Weapon:
    return WEAPONS.get(character.role_info.key, DEFAULT_WEAPON)


def proficiency(level: int) -> int:
    return 2 + (max(1, level) - 1) // 4


def defence(character: Character) -> int:
    return 10 + ARMOUR.get(character.role_info.key, 1) + character.mod("mog")


def attack_bonus(character: Character) -> int:
    return character.mod(weapon(character).stat) + proficiency(character.level)


def damage_notation(character: Character) -> str:
    mod = character.mod(weapon(character).stat)
    return weapon(character).dice + (f"{'+' if mod >= 0 else '-'}{abs(mod)}" if mod else "")


def request(text: str) -> tuple[str, str, int | None, str] | None:
    """("attack"|"damage", "adv"|"dis"|"", target defence or None, reason) for "attack vs 13 adv for the goblin"."""
    match = _REQUEST.match(text)
    if match is None:
        return None
    what = "damage" if match["what"].lower() in ("damage", "dmg") else "attack"
    lean = (match["lean"] or match["lean2"] or "").lower()[:3]
    return what, lean, int(match["ac"]) if match["ac"] else None, (match["reason"] or "").strip()


def roll_damage(notation: str, crit: bool = False, rng: random.Random | None = None) -> tuple[list[int], int, int]:
    """(each die, flat bonus, total) for "1d8+2"; a crit rolls every die twice. The total is at least 1."""
    rng = rng or random.SystemRandom()
    faces, flat, seen = [], 0, ""
    for sign, count, sides, number in _TERM.findall(notation.replace(" ", "")):
        seen += f"{sign}{count}d{sides}" if sides else f"{sign}{number}"
        direction = -1 if sign == "-" else 1
        if sides:
            for _ in range(int(count or 1) * (2 if crit else 1)):
                faces.append(direction * rng.randint(1, int(sides)))
        else:
            flat += direction * int(number)
    if not seen or seen.lstrip("+").lower() != notation.replace(" ", "").lstrip("+").lower() or len(faces) > 40:
        raise CombatError(f"Can't read damage {notation!r}: write it like 1d8+2.")
    return faces, flat, max(1, sum(faces) + flat)


@dataclass
class Attack:
    attacker: str
    target: str
    faces: list[int]
    kept: int
    bonus: int
    defence: int | None
    weapon: str
    damage_dice: str
    damage_faces: list[int] = field(default_factory=list)
    damage_flat: int = 0
    damage: int = 0
    reason: str = ""

    @property
    def total(self) -> int:
        return self.kept + self.bonus

    @property
    def crit(self) -> bool:
        return self.kept == 20

    @property
    def hit(self) -> bool | None:
        """True or False against a known defence; None when the DM will say (no defence given)."""
        if self.kept == 20:
            return True
        if self.kept == 1:
            return False
        return None if self.defence is None else self.total >= self.defence


def strike(attacker: str, target: str, bonus: int, damage_dice: str, defence: int | None, lean: str = "",
           weapon_name: str = "", reason: str = "", rng: random.Random | None = None) -> Attack:
    """One attack: roll to hit, and roll damage when it lands (or when nobody knows the defence yet)."""
    rng = rng or random.SystemRandom()
    faces = [rng.randint(1, 20) for _ in range(2 if lean in ("adv", "dis") else 1)]
    kept = max(faces) if lean == "adv" else min(faces) if lean == "dis" else faces[0]
    result = Attack(attacker, target, faces, kept, bonus, defence, weapon_name, damage_dice, reason=reason)
    if result.hit is not False:
        result.damage_faces, result.damage_flat, result.damage = roll_damage(damage_dice, result.crit, rng)
    return result


def character_strike(character: Character, target: str, defence: int | None, lean: str = "", extra: str = "",
                     reason: str = "", rng: random.Random | None = None) -> Attack:
    """A character attacks with their class weapon; `extra` adds context dice ("1d6" for striking unseen)."""
    dice = damage_notation(character) + (f"+{extra.lstrip('+')}" if extra else "")
    return strike(character.name, target, attack_bonus(character), dice, defence, lean, weapon(character).name, reason, rng)


def _faces(attack: Attack) -> str:
    if len(attack.faces) == 1:
        return f"[{attack.faces[0]}]"
    kept_at = attack.faces.index(attack.kept)
    return "[" + ", ".join(str(f) if i == kept_at else f"~~{f}~~" for i, f in enumerate(attack.faces)) + "]"


def render(attack: Attack, who: str) -> str:
    """Headline first (hit and damage, or the miss), then the arithmetic in subtext."""
    against = f" vs defence {attack.defence}" if attack.defence is not None else ""
    damage = ""
    if attack.damage_faces or attack.damage:
        dice = ", ".join(str(f) for f in attack.damage_faces)
        damage = f" · {attack.weapon or 'damage'} {attack.damage_dice}{' ×2 dice' if attack.crit else ''} [{dice}] {signed(attack.damage_flat)}"
    target = f" → {attack.target}" if attack.target else ""
    if attack.hit is None:
        headline = f"**{attack.total}** to hit · **{attack.damage}** damage if it lands"
    elif attack.crit:
        headline = f"**Critical hit.** **{attack.damage}** damage"
    elif attack.hit:
        headline = f"**Hit.** **{attack.damage}** damage"
    else:
        headline = "**Miss.**" if attack.kept != 1 else "**Miss.** Spawnpilled."
    reason = f" for **{attack.reason}**" if attack.reason else ""
    return (f"{headline} · {attack.attacker}{target}\n"
            f"-# {attack.attacker} ({who}) attacked{reason} · {_faces(attack)} {signed(attack.bonus)} = {attack.total}{against}{damage}")


def render_damage(character: Character, faces: list[int], flat: int, total: int, who: str, crit: bool = False) -> str:
    w = weapon(character)
    return (f"**{total}** damage · {w.name}\n"
            f"-# {character.name} ({who}) rolled {damage_notation(character)}{' ×2 dice' if crit else ''} · "
            f"[{', '.join(map(str, faces))}] {signed(flat)}")


def die_sides(notation: str) -> int:
    sides = [int(s) for _sign, _count, s, _n in _TERM.findall(notation) if s]
    return max(sides) if sides else 6

