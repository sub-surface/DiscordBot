import asyncio
import random
import dataclasses
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import discord

from psychograph import digest, duel, heartbeat
from psychograph.cogs.fun import parse_hours, verdict_card
from psychograph.conversation import quote_other_personas
from psychograph.jev import DebateNotes, Flag
from psychograph.quick import QUICKS, QuickRequest, card, record

from .helpers import FakeBackend, make_bot


def choice(option: str, probabilities: dict, confidence: float = 0.8) -> dict:
    return {"type": "choice", "choice": option, "confidence": confidence, "probabilities": probabilities}


def message(name: str, text: str, minutes_ago: int, bot: bool = False, reactions: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        id=1000 + minutes_ago,
        author=SimpleNamespace(id=7, name=name, display_name=name, bot=bot),
        clean_content=text, content=text, embeds=[], attachments=[], webhook_id=None,
        created_at=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago),
        reactions=[SimpleNamespace(count=reactions)] if reactions else [],
        jump_url=f"https://discord.com/channels/10/1/{1000 + minutes_ago}",
    )


class FakeChannel:
    def __init__(self, name: str, messages: list) -> None:
        self.id, self.name, self.messages = 1, name, messages   # newest first, like Discord
        self.guild = SimpleNamespace(id=10)
        self.parent = None
        self.send = AsyncMock(side_effect=lambda **kwargs: SimpleNamespace(id=9000, **kwargs))

    async def history(self, limit=100, before=None, after=None, around=None):
        if around is not None:
            return
        for item in self.messages[:limit]:
            yield item


class DuelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = make_bot()
        self.aura, self.dot = self.bot.personas.get(10, "aura"), self.bot.personas.get(10, "dot")
        self.turns = [duel.Turn(self.aura, "the form of the hot dog is a sandwich"), duel.Turn(self.dot, "production says taco")]

    def tearDown(self) -> None:
        self.bot.store.close()

    def test_turns_see_the_debate_so_far(self) -> None:
        context, instruction = duel.instruction(self.dot, self.aura, "is a hot dog a sandwich", self.turns[:1], 1, 2)

        self.assertIn("against aura on: is a hot dog a sandwich", context)
        self.assertIn("aura: the form of the hot dog", context)
        self.assertEqual(instruction, "Round 1 of 2. Your turn, dot.")

    def test_jev_picks_a_winner_and_a_line_or_calls_a_draw(self) -> None:
        jev = SimpleNamespace(ask=AsyncMock(return_value={
            "winner": choice("dot", {"dot": 0.7, "aura": 0.2, "even": 0.1}), "best": choice("t0", {}),
        }))
        verdict = asyncio.run(duel.judge(jev, "topic", self.aura, self.dot, self.turns))
        self.assertEqual((verdict.winner.key, verdict.best.persona.key), ("dot", "aura"))
        self.assertEqual(verdict_card(self.aura, self.dot, "topic", verdict).title, "dot wins")

        jev.ask = AsyncMock(return_value={"winner": choice("aura", {"aura": 0.4}, confidence=0.1)})
        self.assertIsNone(asyncio.run(duel.judge(jev, "topic", self.aura, self.dot, self.turns)).winner)

    def test_duel_records_feed_the_table(self) -> None:
        store = self.bot.store
        store.record_duel(10, 1, "aura", "dot", "dot", "a")
        store.record_duel(10, 1, "dot", "ape", None, "b")

        self.assertEqual(store.duel_table(10), [
            {"persona": "dot", "wins": 1, "losses": 0, "draws": 1},
            {"persona": "ape", "wins": 0, "losses": 0, "draws": 1},
            {"persona": "aura", "wins": 0, "losses": 1, "draws": 0},
        ])


class HeartbeatTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeBackend("zack: 41mm. leg day is every day.")
        self.bot = make_bot(self.backend)
        self.bot.webhooks = MagicMock(available=MagicMock(return_value=False))

    def tearDown(self) -> None:
        self.bot.store.close()

    def test_slots_are_spread_through_the_window_and_stable(self) -> None:
        day = date(2026, 10, 7)
        times = heartbeat.slots(day, "leg-day", 3, 13, 1)

        self.assertEqual(times, heartbeat.slots(day, "leg-day", 3, 13, 1))
        self.assertTrue(all(datetime(2026, 10, 7, 13, tzinfo=timezone.utc) <= t < datetime(2026, 10, 8, 1, tzinfo=timezone.utc) for t in times))
        self.assertEqual(len(heartbeat.due(times[1] + timedelta(minutes=3), "leg-day", 3, 13, 1)), 1)
        self.assertEqual(heartbeat.due(times[1] + timedelta(minutes=20), "leg-day", 3, 13, 1), [])
        self.assertEqual(parse_hours("13-1"), (13, 1))

    def test_a_dead_channel_is_left_alone(self) -> None:
        channel = FakeChannel("leg-day", [message("Leon", "gym later?", 60 * 20)])
        self.bot.jev.ask = AsyncMock()

        self.assertIsNone(asyncio.run(heartbeat.drop(self.bot, channel)))
        self.bot.jev.ask.assert_not_awaited()
        self.assertEqual(self.backend.calls, [])

    def test_a_quiet_channel_only_gets_a_loose_end_picked_up(self) -> None:
        channel = FakeChannel("leg-day", [message("Leon", "is 41mm good for a shin", 300)])
        self.bot.jev.ask = AsyncMock(return_value={"opening": {"noul": 0.3}, "who": choice("aura", {})})
        self.assertIsNone(asyncio.run(heartbeat.drop(self.bot, channel)))  # wrapped up: nothing

        self.bot.jev.ask = AsyncMock(return_value={"opening": {"noul": 0.8}, "who": choice("aura", {})})
        sent = asyncio.run(heartbeat.drop(self.bot, channel))

        question = self.bot.jev.ask.await_args_list[0].args[1]["opening"]
        self.assertIn("loose end", question["instructions"])
        self.assertIn("went quiet 5 hours ago", self.backend.calls[0][-1]["content"])
        self.assertTrue(sent.embed.description.startswith("-# ↪ [Leon](<https://discord.com/channels/10/1/1300>)\n"))
        self.assertEqual(self.bot.store.message(sent.id)["persona"], "aura")

    def test_an_active_channel_only_gets_a_drop_in_when_jev_sees_an_opening(self) -> None:
        channel = FakeChannel("shitpost", [message("Leon", "plato was a vibecoder", 2), message("Charlie", "skill issue", 5)])
        self.bot.jev.ask = AsyncMock(return_value={"opening": {"noul": 0.2}, "who": choice("aura", {})})
        self.assertIsNone(asyncio.run(heartbeat.drop(self.bot, channel)))
        self.assertEqual(self.backend.calls, [])

        self.bot.jev.ask = AsyncMock(return_value={"opening": {"noul": 0.9}, "who": choice("aura", {})})
        sent = asyncio.run(heartbeat.drop(self.bot, channel))
        self.assertEqual(self.bot.store.message(sent.id)["persona"], "aura")
        self.assertIn("Charlie: skill issue", self.backend.calls[0][-1]["content"])

    def test_lore_is_handed_over_only_when_jev_is_sure_the_chat_calls_back_to_it(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            lore = Path(folder) / "lore.json"
            lore.write_text(json.dumps({"exhibit": "Zack is the convention's newest exhibit"}), encoding="utf-8")
            self.bot.settings = dataclasses.replace(self.bot.settings, lore_file=lore)
            channel = FakeChannel("shitpost", [message("Leon", "zack is on display again", 2)])

            self.bot.jev.ask = AsyncMock(return_value={"opening": {"noul": 0.9}, "who": choice("aura", {}), "lore": choice("exhibit", {}, 0.4)})
            asyncio.run(heartbeat.drop(self.bot, channel))
            self.assertIn("none", self.bot.jev.ask.await_args_list[0].args[1]["lore"]["criteria"])
            self.assertNotIn("newest exhibit", self.backend.calls[-1][-1]["content"])

            self.bot.jev.ask = AsyncMock(return_value={"opening": {"noul": 0.9}, "who": choice("aura", {}), "lore": choice("exhibit", {}, 0.9)})
            asyncio.run(heartbeat.drop(self.bot, channel))
            self.assertIn("newest exhibit", self.backend.calls[-1][-1]["content"])

    def free_rng(self) -> random.Random:
        """Always takes the free path, trying the kinds in order: stock line, receipt, vibe."""
        rng = random.Random()
        rng.random, rng.shuffle = (lambda: 0.0), (lambda items: None)
        return rng

    def test_a_free_drop_posts_a_stock_line_without_the_model(self) -> None:
        line = self.bot.personas.get(None, "aura").says[0]
        channel = FakeChannel("shitpost", [message("Leon", "zack just got clamped again", 2)])
        self.bot.jev.ask = AsyncMock(side_effect=[
            {"opening": {"noul": 0.9}, "who": choice("aura", {})},
            {"line": choice(line, {}, 0.9)},
        ])
        sent = asyncio.run(heartbeat.drop(self.bot, channel, self.free_rng()))

        self.assertEqual(sent.embed.description, line)
        self.assertEqual(self.backend.calls, [])

    def test_a_due_prediction_gets_one_receipt(self) -> None:
        old = discord.utils.time_snowflake(datetime.now(timezone.utc) - timedelta(days=30))
        self.bot.store.add_prediction(old, 10, 1, 42, "zack gets banned by friday", 7)
        channel = FakeChannel("shitpost", [message("Leon", "anyway", 2)])
        unsure = {"opening": {"noul": 0.9}, "who": choice("aura", {}), "line": choice("none", {}, 0.9)}
        self.bot.jev.ask = AsyncMock(return_value=unsure)

        sent = asyncio.run(heartbeat.drop(self.bot, channel, self.free_rng()))
        self.assertIn("receipt due", sent.embed.description)
        self.assertIn("zack gets banned by friday", sent.embed.description)
        self.assertEqual(self.backend.calls, [])

        asyncio.run(heartbeat.drop(self.bot, channel, self.free_rng()))  # already resurfaced: nothing free lands
        self.assertEqual(len(self.backend.calls), 1)


class DigestTests(unittest.TestCase):
    def test_digest_counts_the_week_and_skips_the_editorial_without_a_tools_model(self) -> None:
        bot = make_bot()
        channel = FakeChannel("shitpost", [
            message("Leon", "plato was a vibecoder", 60, reactions=7),
            message("Charlie", "skill issue", 90, reactions=2),
            message("Leon", "ok", 120),
            message("mochi", "hewwo", 30, bot=True, reactions=9),
        ])
        guild = SimpleNamespace(id=10, text_channels=[channel])

        post = asyncio.run(digest.build(bot, guild))

        opening, reacted, counts = post.split("\n\n")
        self.assertTrue(opening.startswith("**The week to "))  # no editorial on the default profile
        self.assertTrue(reacted.startswith(
            "**Most reacted**\n- **Leon** in #shitpost: [“plato was a vibecoder”](<https://discord.com/channels/10/1/1060>) · 7"
        ))
        self.assertNotIn("hewwo", post)
        self.assertTrue(counts.startswith("-# 3 messages · most active: Leon 2, Charlie 1 · busiest: #shitpost (3)"))
        self.assertEqual(digest.voice(bot, 10).key, "minutes")
        self.assertEqual(digest.week_key(datetime(2026, 10, 5, tzinfo=timezone.utc)), "digest:2026-W41")
        bot.store.close()


class SystemOneRecordTests(unittest.TestCase):
    def test_quick_answers_are_kept_with_what_jev_was_shown(self) -> None:
        embed = card("Decision: curry", "`████░░` 80%  curry\n`█░░░░░` 20%  pizza")
        text = record(QUICKS["decide"], QuickRequest(text="pizza or curry", speaker="Leon"), embed)

        self.assertIn("probabilities, not reasons", text)
        self.assertIn("last 15 channel messages", text)
        self.assertIn("Answer: Decision: curry\n80%  curry", text)
        quoted = quote_other_personas([{"role": "assistant", "content": text, "persona": "quick:decide"}], "mochi", lambda k: "Jev, decide")
        self.assertTrue(quoted[0]["content"].startswith("[bot as Jev, decide, quoted for context]"))

    def test_debate_notes_are_kept_for_follow_ups(self) -> None:
        notes = DebateNotes(0.9, (Flag("Zack", "you would say that", "ad hominem", 0.98),), lean="Leon", lean_confidence=0.8)

        self.assertIn('**Zack**: "you would say that" → ad hominem (98%)', notes.record())
        self.assertIn("System 1's own pick, not shown to the reviewer: Leon (80%)", notes.record())


if __name__ == "__main__":
    unittest.main()
