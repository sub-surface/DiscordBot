import asyncio
import random
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

from psychograph import characters as rules
from psychograph import halloween, ledger, tarot
from psychograph import outside as xeno
from psychograph.cogs.chat import ChatCog
from psychograph.cogs.halloween import HalloweenCog, trick_or_treat

from .helpers import FakeDiscord, make_bot


class Halloween(unittest.TestCase):
    """Switches Halloween to `mode` for the test, and back off after."""
    mode = "on"

    def setUp(self) -> None:
        halloween.configure(self.mode)

    def tearDown(self) -> None:
        halloween.configure("off")


class CalendarTests(Halloween):
    mode = "auto"

    def test_the_season_and_the_night(self) -> None:
        self.assertFalse(halloween.season(date(2026, 10, 23)))
        self.assertTrue(halloween.season(date(2026, 10, 24)))
        self.assertFalse(halloween.night(date(2026, 10, 30)))
        self.assertTrue(halloween.night(date(2026, 10, 31)))
        self.assertFalse(halloween.season(date(2026, 11, 1)))

    def test_yaml_on_and_off_are_read_as_booleans_and_still_work(self) -> None:
        halloween.configure("True")
        self.assertTrue(halloween.night(date(2026, 3, 1)))
        halloween.configure(False)
        self.assertFalse(halloween.season(date(2026, 10, 31)))
        halloween.configure("nonsense")
        self.assertTrue(halloween.night(date(2026, 10, 31)))


class NightTests(Halloween):
    def test_what_counts_as_trick_or_treat(self) -> None:
        for text in ("trick or treat", "Trick or treat!", "trick-or-treat?"):
            self.assertTrue(halloween.request(text), text)
        for text in ("trick or treat yourself", "is trick or treat tonight"):
            self.assertFalse(halloween.request(text), text)

    def test_the_day_card_is_death_and_the_frames_are_pumpkin(self) -> None:
        card, reversed_ = tarot.card_of_the_day(1)
        self.assertEqual((card["name"], reversed_), ("Death", False))
        self.assertEqual(tarot.accent([]), halloween.PUMPKIN)

    def test_only_xenodemons_answer_an_open_call_on_an_orange_numogram(self) -> None:
        rng = random.Random(3)
        kinds = {xeno.summon(rng=rng).kind for _ in range(60)}
        self.assertTrue(all(kind.endswith("Xenodemon") for kind in kinds), kinds)
        self.assertEqual(xeno.accent(xeno.demons()[0]), halloween.PUMPKIN)
        self.assertEqual(xeno.summon("Hugh Chungus"), xeno.demons()[xeno.aq("Hugh Chungus") % 45])  # by name: unchanged

    def test_natural_ones_and_twenties_are_witnessed(self) -> None:
        self.assertIn("applaud", halloween.witnessed("20!", ["crit"]))
        self.assertIn("laughed", halloween.witnessed("1.", ["fumble"]))
        self.assertEqual(halloween.witnessed("7.", ["normal"]), "7.")
        halloween.configure("off")
        self.assertEqual(halloween.witnessed("20!", ["crit"]), "20!")

    def test_a_possessed_answer_comes_from_another_character(self) -> None:
        bot = make_bot()
        halloween.configure("on")
        cog = ChatCog(bot)
        bot._connection.user = SimpleNamespace(id=1, display_name="Mechirk Spookstein")
        channel_persona = bot.personas.for_channel(1, 10)

        async def possess():
            message = FakeDiscord(bot.user).message("hello", 100)
            return cog._possessor(message, channel_persona, 10), message

        possessor, _message = asyncio.run(possess())
        self.assertIsNotNone(possessor)
        self.assertNotEqual(possessor.key, channel_persona.key)
        self.assertEqual((possessor.group, possessor.mode), ("characters", "chat"))
        bot.store.close()


class TrickOrTreatTests(Halloween):
    def setUp(self) -> None:
        super().setUp()
        self.bot = make_bot()
        halloween.configure("on")
        self.bot._connection.user = SimpleNamespace(id=1, display_name="Mechirk Spookstein")
        self.discord = FakeDiscord(self.bot.user)

    def tearDown(self) -> None:
        self.bot.store.close()
        super().tearDown()

    def hero(self, keks: int) -> None:
        character = rules.Character(name="Leon", role="sigma", faction="clank", scores={"mog": 12, "cope": 9, "aura": 14, "lore": 13}, keks=keks)
        ledger.commit(self.bot.store, 10, [ledger.baseline(5, character.to_dict())], "test")

    def knock(self, outcome, day=date(2026, 10, 31)):
        message = self.discord.message("trick or treat", 100, mentioned=False)
        halloween_draw = halloween.draw
        halloween.draw = lambda rng=None: outcome
        try:
            asyncio.run(trick_or_treat(self.bot, message, day))
        finally:
            halloween.draw = halloween_draw
        return message.reply.call_args.args[0]

    def test_once_a_day_each(self) -> None:
        self.knock(("treat", "Mog Mint", 0))
        self.assertIn("already knocked", self.knock(("treat", "Mog Mint", 0)))
        self.assertNotIn("already knocked", self.knock(("treat", "Mog Mint", 0), date(2026, 11, 1)))

    def test_a_treat_goes_into_the_pack(self) -> None:
        self.hero(keks=10)
        text = self.knock(("treat", "Mog Mint", 3))
        self.assertIn("into your pack", text)
        character = self.bot.store.character(10, 5)
        self.assertEqual(character["keks"], 13)
        self.assertIn("Mog Mint", [item["name"] for item in character["inventory"]])

    def test_the_pumpkin_takes_a_kek_but_not_from_empty_pockets(self) -> None:
        self.assertIn("pockets were empty", self.knock(("trick", "pumpkin", 0)))
        self.hero(keks=2)
        self.knock(("trick", "pumpkin", 0), date(2026, 11, 1))
        self.assertEqual(self.bot.store.character(10, 5)["keks"], 1)

    def test_spiders_follow_your_next_three_messages(self) -> None:
        cog = HalloweenCog(self.bot)
        asyncio.run(self.bot.add_cog(cog))
        self.knock(("trick", "spiders", 0))

        async def chat():
            messages = [self.discord.message(f"hi {n}", 200 + n, mentioned=False) for n in range(4)]
            cog.rng = SimpleNamespace(random=lambda: 1.0)  # no hauntings to muddy it
            for message in messages:
                await cog.on_message(message)
            await asyncio.sleep(0)
            return messages

        messages = asyncio.run(chat())
        self.assertEqual([m.add_reaction.await_count for m in messages], [1, 1, 1, 0])


class HauntingTests(Halloween):
    def test_a_haunting_ghost_leaves_again(self) -> None:
        bot = make_bot()
        halloween.configure("on")
        bot._connection.user = SimpleNamespace(id=1, display_name="Mechirk Spookstein")
        cog = HalloweenCog(bot)
        cog.rng = SimpleNamespace(random=lambda: 0.0)
        message = FakeDiscord(bot.user).message("boo", 300, mentioned=False)
        import psychograph.cogs.halloween as module

        async def haunt():
            module.VANISH_SECONDS, saved = 0, module.VANISH_SECONDS
            try:
                await cog.on_message(message)
                await asyncio.sleep(0.01)
            finally:
                module.VANISH_SECONDS = saved

        asyncio.run(haunt())
        message.add_reaction.assert_awaited()
        message.remove_reaction.assert_awaited()
        self.assertFalse(cog._due("haunt", message.channel.id, (1.0, 45)))  # and not again for a while
        bot.store.close()

    def test_nothing_happens_out_of_season(self) -> None:
        bot = make_bot()
        cog = HalloweenCog(bot)
        bot._connection.user = SimpleNamespace(id=1, display_name="Mechirk Spookstein")
        cog.rng = SimpleNamespace(random=lambda: 0.0)
        message = FakeDiscord(bot.user).message("boo", 301, mentioned=False)
        asyncio.run(cog.on_message(message))
        message.add_reaction.assert_not_awaited()
        bot.store.close()


if __name__ == "__main__":
    unittest.main()
