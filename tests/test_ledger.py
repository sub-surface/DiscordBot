import unittest

from psychograph import characters as rules
from psychograph import ledger
from psychograph.ledger import LedgerError
from psychograph.store import Store

GUILD, HUGH, STACK = 10, 1, 2


def hero(name: str = "Foidslayer3000", **changes) -> rules.Character:
    return rules.Character(name=name, role="sigma", faction="clank", scores={"mog": 14, "cope": 12, "aura": 10, "lore": 8}, **changes)


class LedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        ledger.commit(self.store, GUILD, [ledger.baseline(HUGH, hero().to_dict())], "player")
        ledger.commit(self.store, GUILD, [ledger.baseline(STACK, hero("Stackhouse").to_dict())], "player")

    def tearDown(self) -> None:
        self.store.close()

    def get(self, user_id: int) -> rules.Character:
        return rules.Character.from_dict(self.store.character(GUILD, user_id))

    def assertReplayMatches(self, *subjects: int) -> None:
        for subject in subjects:
            cached = self.get(subject).to_dict() if subject else ledger.world_state(self.store, GUILD)
            rebuilt = ledger.replay(self.store, GUILD, subject)
            self.assertEqual(rebuilt.to_dict() if subject else rebuilt, cached)

    def test_a_payment_is_atomic_and_cannot_overdraw(self) -> None:
        ledger.commit(self.store, GUILD, [ledger.keks(HUGH, -40), ledger.keks(STACK, 40)], "player")
        self.assertEqual((self.get(HUGH).keks, self.get(STACK).keks), (60, 140))
        with self.assertRaises(LedgerError):
            ledger.commit(self.store, GUILD, [ledger.keks(STACK, 500), ledger.keks(HUGH, -500)], "player")
        self.assertEqual((self.get(HUGH).keks, self.get(STACK).keks), (60, 140))  # neither half landed
        self.assertReplayMatches(HUGH, STACK)

    def test_items_stack_trade_and_refuse_what_you_dont_have(self) -> None:
        ledger.commit(self.store, GUILD, [ledger.gain(HUGH, "Rope of Hanging", 2, note="from the Dead Chat")])
        ledger.commit(self.store, GUILD, [ledger.gain(HUGH, "rope of hanging")])
        self.assertEqual(self.get(HUGH).inventory, [{"name": "Rope of Hanging", "qty": 3, "kind": "gear", "note": "from the Dead Chat"}])
        ledger.commit(self.store, GUILD, [ledger.lose(HUGH, "ROPE OF HANGING", 3), ledger.gain(STACK, "Rope of Hanging", 3)])
        self.assertEqual((self.get(HUGH).inventory, self.get(STACK).inventory[0]["qty"]), ([], 3))
        with self.assertRaises(LedgerError):
            ledger.commit(self.store, GUILD, [ledger.lose(HUGH, "Rope of Hanging")])
        self.assertReplayMatches(HUGH, STACK)

    def test_clamped_changes_are_stored_as_they_took_effect(self) -> None:
        [heal] = ledger.commit(self.store, GUILD, [ledger.hp(HUGH, 5)])
        self.assertEqual(heal.data["n"], 0)  # already at full
        ledger.commit(self.store, GUILD, [ledger.chuckles(HUGH, 9)])
        self.assertEqual(self.get(HUGH).chuckles, rules.MAX_CHUCKLES)

    def test_undo_cancels_the_whole_transaction_exactly_once(self) -> None:
        [pay, _receive] = ledger.commit(self.store, GUILD, [ledger.keks(HUGH, -25), ledger.keks(STACK, 25)])
        [hit] = ledger.commit(self.store, GUILD, [ledger.hp(HUGH, -30)])
        [trip] = ledger.commit(self.store, GUILD, [ledger.move(HUGH, "deadchat")])
        for entry in (pay, hit, trip):
            ledger.undo(self.store, GUILD, entry.id, "dm")
        hugh = self.get(HUGH)
        self.assertEqual((hugh.keks, self.get(STACK).keks, hugh.hp, hugh.location), (100, 100, hugh.max_hp, "kekwick"))
        with self.assertRaises(LedgerError):
            ledger.undo(self.store, GUILD, pay.id)
        self.assertReplayMatches(HUGH, STACK)

    def test_characters_from_before_the_ledger_get_a_baseline(self) -> None:
        self.store.save_character(GUILD, 3, hero("Old Timer", keks=777).to_dict())
        ledger.commit(self.store, GUILD, [ledger.keks(3, 23)])
        self.assertEqual([r["kind"] for r in self.store.ledger(GUILD, 3)], ["baseline", "keks"])
        self.assertEqual(self.get(3).keks, 800)
        self.assertReplayMatches(3)
        with self.assertRaises(LedgerError):
            ledger.commit(self.store, GUILD, [ledger.keks(99, 5)])  # nobody there

    def test_world_flags_and_clocks(self) -> None:
        ledger.commit(self.store, GUILD, [ledger.clock("lurker", 1, size=6, label="The Lurker King stirs"), ledger.flag("bridge", "burned")])
        [tick] = ledger.commit(self.store, GUILD, [ledger.clock("lurker", 2)])
        state = ledger.world_state(self.store, GUILD)
        self.assertEqual(ledger.clock_bar(state["clocks"]["lurker"]), "◆◆◆◇◇◇")
        ledger.undo(self.store, GUILD, tick.id)
        self.assertEqual(ledger.world_state(self.store, GUILD)["clocks"]["lurker"]["filled"], 1)
        self.assertEqual(ledger.world_state(self.store, GUILD)["flags"], {"bridge": "burned"})
        with self.assertRaises(LedgerError):
            ledger.commit(self.store, GUILD, [ledger.clock("nameless")])  # a new clock needs a size
        self.assertReplayMatches(ledger.WORLD)

    def test_entries_read_as_a_chronicle(self) -> None:
        [entry] = ledger.commit(self.store, GUILD, [ledger.keks(STACK, 50, "kept the oath")])
        self.assertEqual(ledger.describe(entry, "Stackhouse"), "Stackhouse +50 keks · kept the oath")
        [entry] = ledger.commit(self.store, GUILD, [ledger.gain(STACK, "Ban hammer", 2)])
        self.assertEqual(ledger.describe(entry, "Stackhouse"), "Stackhouse gains Ban hammer ×2")


if __name__ == "__main__":
    unittest.main()
