import asyncio
import io
import random
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from PIL import Image

from psychograph import dice, icons

from .helpers import FakeBackend, FakeDiscord, make_bot


class FixedRandom(random.Random):
    """Hands out the given faces in order."""

    def __init__(self, *faces: int) -> None:
        super().__init__()
        self.faces = list(faces)

    def randint(self, low: int, high: int) -> int:
        return self.faces.pop(0)


class NotationTests(unittest.TestCase):
    def test_sums_dice_and_modifiers(self) -> None:
        result = dice.roll("2d6+1d4-2", FixedRandom(3, 5, 4))
        self.assertEqual((result.notation, result.totals), ("2d6+d4-2", [10]))

    def test_keep_and_drop(self) -> None:
        cases = {"4d6kh3": 13, "4d6kl1": 2, "4d6dl1": 13, "4d6dh1": 9}
        for notation, total in cases.items():
            with self.subTest(notation):
                self.assertEqual(dice.roll(notation, FixedRandom(2, 6, 5, 2)).totals, [total])

    def test_advantage_disadvantage_and_natural_flair(self) -> None:
        advantage = dice.roll("adv+5", FixedRandom(20, 3))
        self.assertEqual((advantage.notation, advantage.totals, advantage.natural()), ("2d20kh1+5", [25], 20))
        self.assertIn("natural 20", dice.render(advantage, "Leon"))
        self.assertIn("natural 1", dice.render(dice.roll("dis", FixedRandom(1, 14)), "Leon"))
        self.assertIsNone(dice.roll("2d20", FixedRandom(1, 1)).natural())  # two kept d20s: no single natural

    def test_exploding_percentile_sets_and_reason(self) -> None:
        self.assertEqual(dice.roll("d6!", FixedRandom(6, 6, 2)).totals, [14])
        self.assertEqual(dice.roll("d%", FixedRandom(42)).notation, "d100")
        stats = dice.roll("3x 2d6 for stats", FixedRandom(1, 2, 3, 4, 5, 6))
        self.assertEqual((stats.notation, stats.totals, stats.reason), ("3× 2d6", [3, 7, 11], "stats"))
        self.assertEqual(dice.roll("d20 # stealth", FixedRandom(9)).reason, "stealth")

    def test_bad_notation_says_what_to_type(self) -> None:
        for bad in ("2d6+", "0d6", "1d6kh2", "101d6", "2d6 3d6", "13x d6", "5", "d"):
            with self.subTest(bad), self.assertRaises(dice.DiceError):
                dice.roll(bad)

    def test_render_puts_the_total_first_and_strikes_dropped_dice(self) -> None:
        text = dice.render(dice.roll("4d6kh3 for strength", FixedRandom(2, 6, 5, 2)), "Leon", "<:die_d6:1>")
        self.assertEqual(text.splitlines(), [
            "<:die_d6:1> **13**", "-# Leon rolled `4d6kh3` for **strength**", "-# [2, 6, 5, ~~2~~]",
        ])

    def test_strict_requests_only_match_a_message_that_is_a_roll(self) -> None:
        cases = {
            "roll 2d6+3": "2d6+3", "r d20": "d20", "roll for initiative": "d20 for initiative",
            "roll with it": None, "roll": None, "roll 20": None, "let's roll d20": None, "rolling stones": None,
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(dice.request(text, strict=True), expected)
        self.assertEqual(dice.request("roll"), "d20")  # addressed to the bot, a bare "roll" is a d20
        self.assertEqual(dice.request("roll me a sandwich"), "me a sandwich")  # and gets told why it can't

    def test_result_pictures_show_each_set_and_mark_naturals(self) -> None:
        self.assertEqual(dice.tone(dice.roll("d20", FixedRandom(20))), "crit")
        self.assertEqual(dice.tone(dice.roll("adv", FixedRandom(1, 1))), "fumble")
        self.assertEqual(dice.tone(dice.roll("2d6", FixedRandom(6, 6))), "normal")
        one = icons.roll_image([(20, "17", "normal")])
        stats = icons.roll_image([(6, str(total), "normal") for total in (15, 12, 9, 14, 8, 11, 13)])
        self.assertEqual(one.size, (160, 160))
        self.assertEqual(stats.size, (6 * 120 - 8, 2 * 120 - 8))  # six to a row

    def test_a_roll_written_into_roleplay_is_found(self) -> None:
        cases = {
            "This door, we have them in the courts. Doors that won't admit chuds. Roll lore": "lore",
            "I take a run at it." + chr(10) + "roll mog for the door": "mog for the door",
            "Roll 2d6+3! Take that.": "2d6+3",
            "I'll roll with it. We should roll out later.": None,
            "rolling stones are overrated": None,
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(dice.embedded_request(text), expected)

    def test_die_icons_are_drawn_for_every_die(self) -> None:
        for name, (draw, _fallback) in icons.ICONS.items():
            with Image.open(io.BytesIO(draw())) as image:
                self.assertEqual(image.size, (icons.SIZE, icons.SIZE), name)
        self.assertEqual(dice.die_icon_name(7), "die_d8")
        self.assertEqual(dice.die_icon_name(100), "die_d20")


class RollRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeBackend("A reply.")
        self.bot = make_bot(self.backend)
        asyncio.run(self.bot.load_cogs())
        self.me = SimpleNamespace(id=999)
        self.bot._connection.user = self.me
        self.cog = self.bot.get_cog("ChatCog")
        self.discord = FakeDiscord(self.me)

    def tearDown(self) -> None:
        self.bot.store.close()

    def say(self, content: str, mentioned: bool) -> SimpleNamespace:
        message = self.discord.message(content, 1000, mentioned=mentioned)
        with patch.object(self.bot.jev, "triage", AsyncMock(return_value=None)) as triage, \
                patch.object(self.bot.ambient, "called", AsyncMock(return_value=None)):
            asyncio.run(self.cog.on_message(message))
        self.triage = triage
        return message

    def test_a_bare_roll_is_answered_without_jev_or_the_model(self) -> None:
        message = self.say("roll 2d6+3 for damage", mentioned=False)

        reply = message.reply.await_args.args[0]
        self.assertIn("rolled `2d6+3` for **damage**", reply)
        self.assertEqual(self.backend.calls, [])
        self.triage.assert_not_awaited()

    def test_a_mention_rolls_for_initiative_with_the_uploaded_icon(self) -> None:
        self.bot.icons["die_d20"] = "<:die_d20:7>"
        message = self.say("<@999> roll for initiative", mentioned=True)

        reply, picture = message.reply.await_args.args[0], message.reply.await_args.kwargs["file"]
        self.assertTrue(reply.startswith("**"))
        self.assertIn("for **initiative**", reply)
        self.assertEqual(picture.filename, "roll.png")  # the result die, drawn
        self.assertEqual(self.backend.calls, [])

    def test_ordinary_chat_about_rolling_is_left_alone(self) -> None:
        message = self.say("we should roll out the update tonight", mentioned=False)

        message.reply.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
