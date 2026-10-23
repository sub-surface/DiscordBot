import asyncio
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

    def test_hostile_messages_parse_in_linear_time(self) -> None:
        import time

        start = time.perf_counter()
        for word in ("attack", "roll attack", "roll perception", "roll d20"):
            for tail in (" " * 5000 + "x", " vs" + " " * 3000 + "13 " + "adv " * 400 + "x", "\t \n" * 2000):
                combat.request(word + tail)
                rules.stat_request(word + tail)
                dice.embedded_request(word + tail)
        self.assertLess(time.perf_counter() - start, 0.5)


class TarotTests(unittest.TestCase):
    def test_the_days_card_is_the_same_all_day_and_differs_by_server(self) -> None:
        day = date(2026, 10, 18)
        self.assertEqual(tarot.card_of_the_day(1, day), tarot.card_of_the_day(1, day))
        days = {tarot.card_of_the_day(1, date(2026, 10, d))[0]["key"] for d in range(1, 29)}
        self.assertGreater(len(days), 10)

    def test_readings_are_layouts_with_the_linked_art(self) -> None:
        async def build():
            return tarot.reading("three", 1, "Hugh Chungus", "will the bot ever be finished", random.Random(3)).view()

        import asyncio
        view = asyncio.run(build())
        self.assertIsInstance(view, discord.ui.LayoutView)
        [container] = view.children
        gallery = next(child for child in container.children if isinstance(child, discord.ui.MediaGallery))
        self.assertEqual(len(gallery.items), 3)
        self.assertTrue(all(item.media.url.startswith("https://upload.wikimedia.org/") for item in gallery.items))
        self.assertEqual(len(tarot.deck()), 78)

    def test_quick_readings_are_asked_for_in_chat(self) -> None:
        self.assertEqual(tarot.request("draw a card", strict=True), ("one", ""))
        self.assertEqual(tarot.request("tarot: will I get the job", strict=True), ("one", "will I get the job"))
        self.assertEqual(tarot.request("pull me three cards about the raid", strict=True), ("three", "the raid"))
        self.assertEqual(tarot.request("card of the day", strict=True), ("today", ""))
        self.assertEqual(tarot.request("draw a card will I win?"), ("one", "will I win?"))
        for chat in ("tarot is cringe", "draw a card will I win?", "drawing a card", "I love tarot"):
            self.assertIsNone(tarot.request(chat, strict=True), chat)

    def test_a_question_cannot_ping_anyone(self) -> None:
        async def build():
            return tarot.reading("one", 1, "Hugh", "@everyone <@&1475646287507361902> will it work", random.Random(1)).view()

        import asyncio
        [container] = asyncio.run(build()).children
        heading = container.children[0].content
        self.assertNotIn("@everyone", heading.replace("@​everyone", ""))
        self.assertNotIn("<@&1475646287507361902>", heading)


class TarotDealTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from unittest import mock

        patcher = mock.patch.object(tarot, "FLIP_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def urls(view) -> list[str]:
        [container] = view.children
        gallery = next(child for child in container.children if isinstance(child, discord.ui.MediaGallery))
        return [item.media.url for item in gallery.items]

    @staticmethod
    def texts(view) -> str:
        [container] = view.children
        return "\n".join(child.content for child in container.children if isinstance(child, discord.ui.TextDisplay))

    async def test_cards_are_dealt_face_down_then_turned_one_at_a_time(self) -> None:
        reading = tarot.reading("three", 1, "Hugh", "", random.Random(3))
        posted = []

        async def send(view):
            posted.append(view)

        await tarot.deal(reading, send, send)
        faces = [card["image"] for card, _ in reading.cards]
        self.assertEqual([self.urls(view) for view in posted], [
            [tarot.back()] * 3, faces[:1] + [tarot.back()] * 2, faces[:2] + [tarot.back()], faces,
        ])
        self.assertNotIn(reading.cards[0][0]["name"], self.texts(posted[0]))  # nothing given away face down
        self.assertEqual(int(posted[0].children[0].accent_colour), tarot.ACCENT)

    async def test_a_deleted_message_ends_the_deal_quietly(self) -> None:
        reading = tarot.reading("three", 1, "Hugh", "", random.Random(3))
        edits = 0

        async def send(view):
            return None

        async def edit(view):
            nonlocal edits
            edits += 1
            raise discord.NotFound(type("Response", (), {"status": 404, "reason": "Not Found"})(), "Unknown Message")

        async def take():
            await asyncio.sleep(10)
            return "never"

        pending = take()
        await tarot.deal(reading, send, edit, pending)
        self.assertEqual(edits, 1)

    async def test_mini_mes_take_goes_under_the_cards_and_a_failed_one_clears_the_wait(self) -> None:
        reading = tarot.reading("one", 1, "Hugh", "will it work", random.Random(1))
        for line, expected in (("Ask again after the patch.", "Ask again after the patch."), ("", None)):
            posted = []

            async def send(view):
                posted.append(view)

            async def take():
                return line

            await tarot.deal(reading, send, send, take())
            self.assertIn("squints", self.texts(posted[-2]))
            self.assertNotIn("squints", self.texts(posted[-1]))
            if expected:
                self.assertIn(expected, self.texts(posted[-1]))

    async def test_the_take_is_off_by_default_switchable_and_once_a_minute_each(self) -> None:
        from tests.helpers import FakeBackend, make_bot

        bot = make_bot(FakeBackend("@everyone the Tower says no."))
        tarot._last_take.clear()
        self.assertFalse(tarot.wants_take(bot, 1))
        bot.store.set_state("tarot:mini_me", "on")
        self.assertTrue(tarot.wants_take(bot, 1))
        self.assertFalse(tarot.wants_take(bot, 1))  # the same person again within the minute
        self.assertTrue(tarot.wants_take(bot, 2))
        line = await tarot.take(bot, tarot.reading("one", 1, "Hugh", "will it work", random.Random(1)))
        self.assertNotIn("@everyone", line.replace("@\u200beveryone", ""))
        self.assertEqual(bot.backend.thinking_calls, [False])


class TarotColourTests(unittest.TestCase):
    def test_the_frame_takes_the_suit_colour(self) -> None:
        cups, wands, swords = ({"suit": suit} for suit in ("Cups", "Wands", "Swords"))
        major = {"arcana": "major"}
        self.assertEqual(tarot.accent([]), tarot.ACCENT)
        self.assertEqual(tarot.accent([(major, False)]), tarot.ACCENT)
        self.assertEqual(tarot.accent([(cups, True)]), tarot.SUIT_ACCENTS["Cups"])
        self.assertEqual(tarot.accent([(wands, False), (swords, False), (wands, False)]), tarot.SUIT_ACCENTS["Wands"])
        self.assertEqual(tarot.accent([(wands, False), (cups, False), (swords, False)]), tarot.SUIT_ACCENTS["Cups"])

    def test_every_card_has_a_known_suit_or_is_major(self) -> None:
        for card in tarot.deck():
            self.assertTrue(card["arcana"] == "major" or card["suit"] in tarot.SUIT_ACCENTS, card["name"])
        self.assertTrue(tarot.back().startswith("https://upload.wikimedia.org/"))


class RerollTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_double_click_spends_one_chuckle(self) -> None:
        from unittest import mock

        from psychograph import ledger
        from psychograph.cogs.party import RerollView
        from tests.helpers import make_bot

        bot = make_bot()
        ledger.commit(bot.store, 1, [ledger.baseline(7, sigma(chuckles=2).to_dict())])
        rolls = []

        async def redo():
            rolls.append(1)
            return "**12** · Yes, but…", None, False

        view = RerollView(bot, 1, 7, redo)
        clicks = []
        for _ in range(2):
            click = mock.MagicMock()
            click.response.edit_message = mock.AsyncMock()
            click.response.defer = mock.AsyncMock()
            click.response.send_message = mock.AsyncMock()
            clicks.append(click)
        with mock.patch("psychograph.cogs.party.act"):
            for click in clicks:
                await view.reroll.callback(click)
        self.assertEqual((len(rolls), bot.store.character(1, 7)["chuckles"]), (1, 1))
        clicks[1].response.defer.assert_awaited()
        bot.store.close()


if __name__ == "__main__":
    unittest.main()
