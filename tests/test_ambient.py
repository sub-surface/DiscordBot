import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from psychograph.ambient import Ambient, call_names, first_named

from .helpers import FakeBackend, FakeDiscord, make_bot

NOW = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def emoji(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, available=True)


def jev_says(reacts: float = 0.0, mood: str | None = None, addressed: float = 0.0) -> AsyncMock:
    answers = {"reacts": {"noul": reacts}, "addressed": {"noul": addressed}}
    if mood:
        answers["mood"] = {"choice": mood, "confidence": 0.7}
    return AsyncMock(return_value=answers)


class AmbientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeBackend("Hello from the model.")
        self.bot = make_bot(self.backend)
        asyncio.run(self.bot.load_cogs())
        self.me = SimpleNamespace(id=999)
        self.bot._connection.user = self.me
        self.bot.jev.api_key = "test"
        self.bot.webhooks.member_has_name = AsyncMock(return_value=False)
        self.cog = self.bot.get_cog("ChatCog")
        self.discord = FakeDiscord(self.me)
        self.emojis = [emoji("KEKW"), emoji("laoma"), emoji("mmm"), emoji("pepethink"), emoji("hmmm")]
        self.now = 1000.0
        self.bot.ambient = Ambient(self.bot, clock=lambda: self.now)

    def tearDown(self) -> None:
        self.bot.store.close()

    def say(self, text: str, message_id: int, at: datetime = NOW):
        message = self.discord.message(text, message_id, mentioned=False)
        message.guild = SimpleNamespace(id=10, emojis=self.emojis)
        message.created_at = at
        asyncio.run(self.cog.on_message(message))
        return message

    def test_chatters_answer_to_name_bot_and_characters_to_their_name(self) -> None:
        aura, mochi = self.bot.personas.get(10, "aura"), self.bot.personas.get(10, "mochi")
        names = {name: persona for persona in (aura, mochi) for name in call_names(persona)}

        self.assertEqual(call_names(aura), ["aura-bot"])
        self.assertEqual(first_named("ok AURA-BOT, what would mochi say", names), ("aura-bot", aura))
        self.assertIsNone(first_named("that was such an aura move, mochis", names))

    def test_name_bot_summons_the_chatter_without_asking_jev(self) -> None:
        self.bot.jev.ask = jev_says()

        self.say("aura-bot what do you make of this", 1000)

        self.assertIn("aura", self.backend.calls[0][0]["content"].casefold())
        self.assertEqual(self.bot.store.message(2000)["persona"], "aura")
        self.bot.jev.ask.assert_not_awaited()  # "-bot" is unambiguous: no addressed check

    def test_a_bare_chatter_name_is_the_member_not_the_bot(self) -> None:
        self.bot.jev.ask = jev_says(addressed=0.99)
        self.say("aura what do you make of this", 1000)
        self.assertEqual(self.backend.calls, [])

    def test_a_character_is_answered_by_name_only_when_jev_is_sure(self) -> None:
        self.bot.jev.ask = jev_says(addressed=0.2)
        self.say("that is such a mochi thing to say", 1000)
        self.assertEqual(self.backend.calls, [])

        self.now += 120
        self.bot.jev.ask = jev_says(addressed=0.93)
        self.say("mochi what do you make of this", 1001)
        self.assertEqual(self.bot.store.message(2000)["persona"], "mochi")

    def test_the_persona_reacts_with_an_emote_in_the_mood_that_fits(self) -> None:
        self.bot.jev.ask = jev_says(reacts=0.95, mood="funny")

        first = self.say("i benched 140 today and cried after", 1000)
        self.now += 120
        second = self.say("i benched 150 today and cried after", 1001)

        first.add_reaction.assert_awaited_once_with(self.emojis[0])
        second.add_reaction.assert_not_awaited()  # within the cooldown
        criteria = self.bot.jev.ask.await_args.args[1]["mood"]["criteria"]
        self.assertEqual(set(criteria), {"funny", "zen", "thinking"})  # moods the server has an emote for
        self.assertEqual(self.backend.calls, [])

    def test_favourite_emotes_are_marked_for_jev(self) -> None:
        self.bot.store.update_channel(1, persona="aura")
        self.bot.jev.ask = jev_says(reacts=0.9, mood="zen")

        message = self.say("just made tea and watched the rain for an hour", 1000)

        message.add_reaction.assert_awaited_once_with(self.emojis[1])
        self.assertIn("A favourite of aura", self.bot.jev.ask.await_args.args[1]["mood"]["criteria"]["zen"])

    def test_short_unremarkable_or_switched_off_gets_no_reaction(self) -> None:
        self.bot.jev.ask = jev_says(reacts=0.4, mood="funny")
        self.say("lol", 1000)
        self.bot.jev.ask.assert_not_awaited()
        self.assertFalse(self.say("anyone around this evening", 1001).add_reaction.await_count)

        self.now += 120
        self.bot.store.update_channel(1, reactions=False)
        self.bot.jev.ask = jev_says(reacts=0.99, mood="funny")
        self.say("this is the funniest thing anyone has said", 1002)
        self.bot.jev.ask.assert_not_awaited()

    def test_the_thinking_status_is_a_random_thinking_emote(self) -> None:
        guild = SimpleNamespace(id=10, emojis=self.emojis)
        drawn = {self.bot.ambient.thinking(guild).name for _ in range(40)}

        self.assertEqual(drawn, {"pepethink", "hmmm"})
        self.assertIsNone(self.bot.ambient.thinking(SimpleNamespace(id=10, emojis=[])))
        self.assertIn(self.bot.responder.status(guild).name, drawn)
        self.assertEqual(self.bot.responder.status(None), "👀")

    def test_four_quiet_days_then_a_message_gets_the_revival_emote(self) -> None:
        self.bot.jev.ask = jev_says()
        self.say("anyone still here", 1000, at=NOW - timedelta(days=5))

        revived = self.say("hello?? is this place dead", 1001)

        revived.add_reaction.assert_awaited_once_with(self.emojis[2])


if __name__ == "__main__":
    unittest.main()
