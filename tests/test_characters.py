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
    base = dict(name="Theophant the Unshirted", role="sigma", faction="iron",
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
        self.assertEqual(rules.stat_request("cope for the door"), ("cope", "", "the door", "COPE"))
        self.assertEqual(rules.stat_request("MOG adv"), ("mog", "adv", "", "MOG"))
        self.assertEqual(rules.stat_request("lore # history"), ("lore", "", "history", "LORE"))
        self.assertEqual(rules.stat_request("perception adv for traps"), ("lore", "adv", "traps", "Perception (LORE)"))
        self.assertEqual(rules.stat_request("initiative"), ("mog", "", "", "Initiative (MOG)"))
        self.assertEqual(rules.stat_request("save cope"), ("cope", "", "", "COPE save"))
        self.assertEqual(rules.stat_request("aura save dis"), ("aura", "dis", "", "AURA save"))
        self.assertEqual(dice.request("roll stealth for the guards", strict=True), "stealth for the guards")
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
        rules.check = lambda character, stat, lean="", reason="", label="", rng=None: random_check(
            character, stat, lean, reason, label, FixedRandom(12))
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


class WorldTests(unittest.TestCase):
    def test_places_roads_and_the_crier(self) -> None:
        from psychograph import world

        self.assertEqual(world.find_place("the cringe tavern").key, "cringe")
        self.assertEqual(world.find_place("Dead Chat").key, "deadchat")
        self.assertIsNone(world.find_place("narnia"))
        self.assertEqual(world.place("nowhere").key, "kekwick")
        self.assertTrue(all(a in world.PLACES and b in world.PLACES for a, b in world.ROADS))
        line = world.crier_line(random.Random(3))
        self.assertTrue(line.startswith("**The Crier** · "))

    def test_map_draws_people_at_their_places(self) -> None:
        from psychograph import world

        png = world.map_png([("Foidslayer3000", "cringe", None), ("A Wanderer", "deadchat", b"not an image"), ("Lost", "atlantis", None)])
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.size, (world.W, world.H))

    def test_classic_classes_and_a_retired_one(self) -> None:
        self.assertEqual({role.classic for role in rules.CLASSES.values()} >= {"Fighter", "Rogue", "Wizard", "Cleric", "Bard", "Ranger"}, True)
        self.assertEqual(hero(role="arbiter").role_info.key, "sigma")  # the Yellow Card class, retired

    def test_contest_text_names_the_winner_or_a_stalemate(self) -> None:
        from psychograph.cogs.party import contest_text

        a, b = hero(), hero(name="Stackhouse", role="liturgist")
        win = contest_text(a, b, "COPE", rules.Check("cope", [15], 15, 3, ""), rules.Check("cope", [4], 4, 0, ""), "Hugh", "Stack")
        tie = contest_text(a, b, "COPE", rules.Check("cope", [10], 10, 0, ""), rules.Check("cope", [10], 10, 0, ""), "Hugh", "Stack")
        self.assertIn("**Theophant the Unshirted** wins", win)
        self.assertIn("stalemate", tie)
