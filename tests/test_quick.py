import asyncio
import unittest
from unittest.mock import AsyncMock

from psychograph import quick
from psychograph.conversation import Said
from psychograph.jev import Jev
from psychograph.repeats import RepeatGuard, normalise


def choice(option: str, probabilities: dict) -> dict:
    return {"type": "choice", "choice": option, "confidence": 0.9, "probabilities": probabilities}


class QuickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.jev = Jev("test-key")

    def run_feature(self, key: str, answers: dict | None, **request) -> object:
        self.jev.ask = AsyncMock(return_value=answers)
        return asyncio.run(quick.QUICKS[key].run(self.jev, quick.QuickRequest(speaker="Leon", **request)))

    def test_triggers_and_options_are_read_from_plain_text(self) -> None:
        feature, rest = quick.quick_for("decide: should we get pizza or curry or a protein shake?")
        self.assertEqual((feature.key, quick.split_options(rest)), ("decide", ["pizza", "curry", "a protein shake"]))
        self.assertEqual(quick.quick_for("tier list: crocs, tea")[0].key, "tier")
        self.assertIsNone(quick.quick_for("decided to stay in tonight"))
        self.assertEqual(quick.split_items("crocs, tea\ncrocs; jury duty"), ["crocs", "tea", "jury duty"])

    def test_decide_shows_every_option_with_its_odds(self) -> None:
        embed = self.run_feature("decide", {"pick": choice("curry", {"pizza": 0.2, "curry": 0.8})}, text="pizza or curry")

        self.assertEqual(embed.title, "Decision: curry")
        self.assertLess(embed.description.index("curry"), embed.description.index("pizza"))
        self.assertIn("no model call", embed.footer.text)

    def test_features_explain_what_they_need_without_calling_jev(self) -> None:
        self.assertIn("at least two", self.run_feature("decide", None, text="pizza").description)
        self.jev.ask.assert_not_awaited()
        self.assertIn("at least three", self.run_feature("chatter", None, text="").description)

    def test_tier_list_groups_items_by_tier(self) -> None:
        answers = {"t0": choice("A", {}), "t1": choice("D", {}), "t2": choice("A", {})}
        embed = self.run_feature("tier", answers, text="tea, crocs, the moon landing")

        self.assertEqual(embed.description.splitlines(), ["**A**  tea, the moon landing", "**D**  crocs"])

    def test_an_unreachable_jev_says_so(self) -> None:
        self.assertEqual(self.run_feature("odds", None, text="will it rain").title, "Couldn't ask Jev")

    def test_vibe_names_a_main_character_from_members_only(self) -> None:
        said = [Said("Zack", "41mm", 6), Said("zack", "commit this", None)] * 3
        answers = {
            "heat": {"score": 2.0}, "chaos": {"score": 1.0}, "fun": {"noul": 0.6},
            "main": choice("Zack", {}),
        }
        embed = self.run_feature("vibe", answers, text="", said=said)

        self.assertIn("Main character: **Zack**", embed.description)
        self.assertNotIn("main", self.jev.ask.await_args.args[1])  # one member: nobody to compare


class RepeatGuardTests(unittest.TestCase):
    def test_repeats_match_near_copies_within_the_window(self) -> None:
        now = [1000.0]
        guard = RepeatGuard(clock=lambda: now[0])
        guard.record(1, "<@999> What is the meaning of life?", "https://link")

        self.assertEqual(normalise("<@999> What is the meaning of life?"), "what is the meaning of life")
        self.assertIsNotNone(guard.earlier(1, "what is the meaning of life pls"))
        self.assertIsNone(guard.earlier(2, "what is the meaning of life"))      # another channel
        self.assertIsNone(guard.earlier(1, "what is the meaning of death and taxes"))
        now[0] += 31 * 60
        self.assertIsNone(guard.earlier(1, "what is the meaning of life"))      # long enough ago


if __name__ == "__main__":
    unittest.main()
