import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from psychograph.conversation import Said
from psychograph.jev import DIRECT, NO_CONTEST, SPLIT, Jev
from psychograph.personas import PersonaRegistry
from psychograph.responder import read_verdict
from psychograph.settings import Settings
from psychograph.store import Store


def choice(option: str, confidence: float) -> dict:
    return {"type": "choice", "choice": option, "confidence": confidence, "probabilities": {}}


class JevTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.tools = PersonaRegistry(self.store, Settings().personas_dir, "mochi").tools()
        self.jev = Jev("test-key")

    def tearDown(self) -> None:
        self.store.close()

    def test_without_a_key_nothing_is_called(self) -> None:
        self.assertIsNone(asyncio.run(Jev("").ask("state", {})))

    def test_triage_routes_only_when_confident(self) -> None:
        routes = {**DIRECT, **{tool.key: tool.intent for tool in self.tools}}
        self.jev.ask = AsyncMock(return_value={"kind": choice("judge", 0.95)})
        self.assertEqual(asyncio.run(self.jev.triage("is it wrong to…", "Leon", [], routes)).kind, "judge")
        criteria = self.jev.ask.await_args.args[1]["kind"]["criteria"]
        self.assertEqual(next(iter(criteria)), "chat")

        for answer in (choice("judge", 0.5), choice("chat", 1.0), choice("bot_model", 0.82), choice("chess", 1.0)):
            self.jev.ask = AsyncMock(return_value={"kind": answer})
            self.assertIsNone(asyncio.run(self.jev.triage("hi", "Leon", [], routes)).kind)

    def test_triage_keeps_the_last_few_and_the_relevant_messages(self) -> None:
        said = [Said(f"m{i}", f"message {i}", i) for i in range(20)]

        def keep(answers: dict | None) -> tuple:
            self.jev.ask = AsyncMock(return_value=answers)
            return asyncio.run(self.jev.triage("what time is quali", "Lizzie", said, {})).keep

        relevant = {"needs": {"noul": 0.9}, **{f"r{i}": {"noul": 0.9 if i in (2, 3) else 0.1} for i in range(20)}}
        self.assertEqual(keep(relevant), (2, 3, 14, 15, 16, 17, 18, 19))
        self.assertEqual(keep({"needs": {"noul": 0.1}}), (17, 18, 19))        # stands alone
        self.assertEqual(keep(None), tuple(range(5, 20)))                     # Jev unavailable: the last 15
        state = self.jev.ask.await_args.args[0]
        self.assertEqual(state["latest"], {"speaker": "Lizzie", "text": "what time is quali"})

    def test_triage_shows_the_bot_bracketed(self) -> None:
        self.jev.ask = AsyncMock(return_value={})
        asyncio.run(self.jev.triage("hi", "Zack", [Said("zack", "41mm", None), Said("Zack", "41mm", 6)], {}))
        speakers = [item["speaker"] for item in self.jev.ask.await_args.args[0]["messages"]]
        self.assertEqual(speakers, ["[bot as zack]", "Zack"])

    def test_sounds_play_only_when_the_moment_suits_one(self) -> None:
        sounds = [SimpleNamespace(key="boom", label="boom", description="dramatic boom", mood="dramatic"),
                  SimpleNamespace(key="cheer", label="cheer", description="crowd cheering", mood="hype")]

        def pick(moment: float, sound: dict) -> str | None:
            self.jev.ask = AsyncMock(return_value={"moment": {"type": "noul", "noul": moment}, "sound": sound})
            return asyncio.run(self.jev.pick_sound("plot twist", "and the butler did it", sounds))

        self.assertEqual(pick(0.87, choice("boom", 0.3)), "boom")
        self.assertEqual(pick(0.04, choice("boom", 0.9)), "none")    # a sad or plain moment
        self.assertEqual(pick(0.9, choice("boom", 0.1)), "none")     # no clear pick
        self.jev.ask = AsyncMock(return_value=None)
        self.assertIsNone(asyncio.run(self.jev.pick_sound("a", "b", sounds)))  # couldn't ask: caller falls back

    def test_debate_notes_tag_confident_fouls_and_evidence_from_members_only(self) -> None:
        said = [
            Said("Leon", "CS3 cut injuries by 40% on that route", 5),
            Said("Charlie", "you would say that, you're a cyclist", 6),
            Said("mochi", "hewwo~ dis is a vewy long answer", None),
            Said("Charlie", "nobody uses them in winter anyway though", 6),
        ]
        self.jev.ask = AsyncMock(return_value={
            "debate": {"type": "noul", "noul": 0.9},
            "lean": choice("Leon", 0.8),
            "foul_0": choice("none", 0.8), "evidence_0": {"type": "noul", "noul": 0.96},
            "foul_1": choice("ad_hominem", 0.97), "evidence_1": {"type": "noul", "noul": 0.02},
            "foul_3": choice("moving_goalposts", 0.4), "evidence_3": {"type": "noul", "noul": 0.1},
        })

        notes = asyncio.run(self.jev.debate_notes(said))

        questions = self.jev.ask.await_args.args[1]
        self.assertNotIn("foul_2", questions)  # the persona's own answer isn't tagged
        self.assertEqual(set(questions["lean"]["criteria"]), {"even", "Leon", "Charlie"})
        self.assertFalse(notes.no_debate)
        self.assertEqual([(f.speaker, f.label) for f in notes.flags], [("Leon", "gives evidence"), ("Charlie", "ad hominem")])
        self.assertEqual(notes.lean, "Leon")
        self.assertIn("may be wrong", notes.prompt_block())

    def test_verdicts_are_read_with_or_without_jev(self) -> None:
        self.jev.ask = AsyncMock(return_value={"winner": choice("Leon", 0.99)})
        self.assertEqual(asyncio.run(self.jev.read_verdict("**Decision:** Charlie loses to **Leon**", ["Charlie", "Leon"])), "Leon")
        self.jev.ask = AsyncMock(return_value={"winner": choice("Leon", 0.4)})
        self.assertIsNone(asyncio.run(self.jev.read_verdict("**Decision:** unclear", ["Charlie", "Leon"])))

        self.assertEqual(read_verdict("**Decision:** **Leon**, narrow, over Charlie", ["Charlie", "Leon"]), "Leon")
        self.assertEqual(read_verdict("**Decision:** Split decision.", ["Charlie", "Leon"]), SPLIT)
        self.assertEqual(read_verdict("**Decision:** No contest.", ["Charlie", "Leon"]), NO_CONTEST)
        self.assertIsNone(read_verdict("**Decision:** nobody", ["Charlie", "Leon"]))


if __name__ == "__main__":
    unittest.main()
