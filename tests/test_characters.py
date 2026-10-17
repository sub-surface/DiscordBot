import asyncio
import io
import random
import unittest
from types import SimpleNamespace

from PIL import Image

from psychograph import characters as rules
from psychograph import dice, sheet
from psychograph.cogs.party import roll_reply
from psychograph.store import Store

from .helpers import make_bot


class FixedRandom(random.Random):
    def __init__(self, *faces: int) -> None:
        super().__init__()
        self.faces = list(faces)

    def randint(self, low: int, high: int) -> int:
        return self.faces.pop(0)


def hero(**changes) -> rules.Character:
    base = dict(name="Theophant the Unshirted", role="arbiter", faction="iron",
                scores={"mog": 14, "cope": 17, "aura": 9, "lore": 12}, keepsake=rules.KEEPSAKES[11])
    return rules.Character(**{**base, **changes})


class RulesTests(unittest.TestCase):
    def test_modifiers_and_derived_numbers(self) -> None:
        self.assertEqual([rules.modifier(score) for score in (3, 8, 9, 10, 11, 12, 17, 18)], [-4, -1, -1, 0, 0, 1, 3, 4])
        character = hero()
        self.assertEqual((character.max_hp, character.hp, character.keks, character.mod("cope")), (12, 12, 100, 3))
        self.assertEqual(rules.signed(-1), "−1")

    def test_check_bands_follow_the_one_rule(self) -> None:
        character = hero()
        cases = {(20,): ("crit", 23), (1,): ("fumble", 4), (12,): ("yes", 15), (7,): ("but", 10), (6,): ("miss", 9)}
        for faces, (band, total) in cases.items():
            with self.subTest(faces):
                result = rules.check(character, "cope", rng=FixedRandom(*faces))
                self.assertEqual((result.band[0], result.total), (band, total))
        advantage = rules.check(character, "aura", "adv", rng=FixedRandom(4, 16))
        disadvantage = rules.check(character, "aura", "dis", rng=FixedRandom(4, 16))
        self.assertEqual((advantage.kept, disadvantage.kept), (16, 4))

    def test_stat_requests(self) -> None:
        self.assertEqual(rules.stat_request("cope for the door"), ("cope", "", "the door"))
        self.assertEqual(rules.stat_request("MOG adv"), ("mog", "adv", ""))
        self.assertEqual(rules.stat_request("lore # history"), ("lore", "", "history"))
        self.assertIsNone(rules.stat_request("2d6+3"))
        self.assertEqual(dice.request("roll cope for the door", strict=True), "cope for the door")
        self.assertIsNone(dice.request("roll copes", strict=True))

    def test_spawnpill_gives_four_scores_in_range(self) -> None:
        scores = rules.spawnpill(random.Random(7))
        self.assertEqual(len(scores), 4)
        self.assertTrue(all(3 <= score <= 18 for score in scores))
        self.assertEqual(scores, sorted(scores, reverse=True))

    def test_characters_survive_the_store_and_unknown_fields(self) -> None:
        store = Store(":memory:")
        store.save_character(10, 5, {**hero(keks=412).to_dict(), "from_a_future_version": True})
        loaded = rules.Character.from_dict(store.character(10, 5))
        self.assertEqual((loaded.name, loaded.keks, loaded.faction_info.nickname), ("Theophant the Unshirted", 412, "the Blackpill Brothers"))
        self.assertIsNone(store.character(11, 5))
        self.assertEqual([user for user, _data in store.characters(10)], [5])
        store.close()

    def test_sheet_renders_with_and_without_a_portrait(self) -> None:
        avatar = io.BytesIO()
        Image.new("RGB", (64, 64), (120, 40, 200)).save(avatar, format="PNG")
        for portrait in (None, avatar.getvalue(), b"not an image"):
            with Image.open(io.BytesIO(sheet.png(hero(hp=0), "Hugh Chungus", portrait))) as image:
                self.assertEqual(image.size, (sheet.W, sheet.H))


class StatRollTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = make_bot()

    def tearDown(self) -> None:
        self.bot.store.close()

    def test_a_stat_roll_uses_the_character_and_names_the_band(self) -> None:
        random_check = rules.check
        rules.check = lambda character, stat, lean="", reason="", rng=None: random_check(character, stat, lean, reason, FixedRandom(12))
        try:
            text, picture = roll_reply(self.bot, "cope for the door", "Hugh Chungus", hero())
        finally:
            rules.check = random_check
        self.assertTrue(text.startswith("**15** · Yes."))
        self.assertIn("Theophant the Unshirted (Hugh Chungus) rolled COPE for **the door** · [12] +3", text)
        self.assertEqual(picture.filename, "roll.png")

    def test_without_a_character_a_stat_roll_points_to_creation(self) -> None:
        text, picture = roll_reply(self.bot, "mog", "Hugh Chungus", None)
        self.assertIn("/character create", text)
        self.assertIsNone(picture)


if __name__ == "__main__":
    unittest.main()
