import random
import unittest
from datetime import date

import discord

from psychograph import characters as rules
from psychograph import combat, dice, tarot


class FixedRandom(random.Random):
    """randint returns the queued faces in order."""

    def __init__(self, *faces: int) -> None:
        super().__init__(0)
        self.faces = list(faces)

    def randint(self, a: int, b: int) -> int:
        return self.faces.pop(0)


def sigma(**changes) -> rules.Character:
    return rules.Character(name="Foidslayer3000", role="sigma", faction="clank", scores={"mog": 12, "cope": 9, "aura": 14, "lore": 13}, **changes)


class CombatTests(unittest.TestCase):
    def test_numbers_come_from_class_scores_and_level(self) -> None:
        c = sigma()
        self.assertEqual((combat.attack_bonus(c), combat.damage_notation(c), combat.defence(c)), (4, "1d8+2", 15))
        self.assertEqual(combat.proficiency(5), 3)

    def test_a_hit_rolls_damage_and_a_crit_doubles_the_dice(self) -> None:
        hit = combat.character_strike(sigma(), "a lurker spawn", 13, rng=FixedRandom(10, 5))
        self.assertEqual((hit.total, hit.hit, hit.damage), (14, True, 7))
        crit = combat.character_strike(sigma(), "", 30, rng=FixedRandom(20, 3, 4))
        self.assertEqual((crit.hit, crit.damage_faces, crit.damage), (True, [3, 4], 9))
        self.assertIn("**Critical hit.** **9** damage", combat.render(crit, "Hugh"))

    def test_a_miss_rolls_no_damage_and_a_natural_one_always_misses(self) -> None:
        miss = combat.character_strike(sigma(), "", 18, rng=FixedRandom(9))
        self.assertEqual((miss.hit, miss.damage, combat.render(miss, "Hugh").split(" · ")[0]), (False, 0, "**Miss.**"))
        self.assertFalse(combat.character_strike(sigma(), "", 2, rng=FixedRandom(1)).hit)

    def test_without_a_defence_the_dm_decides(self) -> None:
        open_roll = combat.character_strike(sigma(), "", None, rng=FixedRandom(11, 6))
        self.assertIsNone(open_roll.hit)
        self.assertTrue(combat.render(open_roll, "Hugh").startswith("**15** to hit · **8** damage if it lands"))

    def test_advantage_extra_dice_and_monster_notation(self) -> None:
        result = combat.character_strike(sigma(), "", 10, "adv", extra="1d6", rng=FixedRandom(3, 17, 2, 6))
        self.assertEqual((result.kept, result.damage_faces, result.damage), (17, [2, 6], 10))
        self.assertEqual(combat.roll_damage("2d6-1", rng=FixedRandom(1, 1))[2], 1)  # at least 1
        with self.assertRaises(combat.CombatError):
            combat.roll_damage("a big stick")

    def test_requests_are_heard_in_chat(self) -> None:
        self.assertEqual(combat.request("attack vs 13 adv for the goblin"), ("attack", "adv", 13, "the goblin"))
        self.assertEqual(combat.request("damage"), ("damage", "", None, ""))
        self.assertEqual(dice.request("roll attack vs 12", strict=True), "attack vs 12")
        self.assertIsNone(combat.request("attackers are coming"))


class TarotTests(unittest.TestCase):
    def test_the_days_card_is_the_same_all_day_and_differs_by_server(self) -> None:
        day = date(2026, 10, 18)
        self.assertEqual(tarot.card_of_the_day(1, day), tarot.card_of_the_day(1, day))
        days = {tarot.card_of_the_day(1, date(2026, 10, d))[0]["key"] for d in range(1, 29)}
        self.assertGreater(len(days), 10)

    def test_readings_are_layouts_with_the_linked_art(self) -> None:
        async def build():
            return tarot.reading("three", 1, "Hugh Chungus", "will the bot ever be finished", random.Random(3))

        import asyncio
        view = asyncio.run(build())
        self.assertIsInstance(view, discord.ui.LayoutView)
        [container] = view.children
        gallery = next(child for child in container.children if isinstance(child, discord.ui.MediaGallery))
        self.assertEqual(len(gallery.items), 3)
        self.assertTrue(all(item.media.url.startswith("https://upload.wikimedia.org/") for item in gallery.items))
        self.assertEqual(len(tarot.deck()), 78)


if __name__ == "__main__":
    unittest.main()
