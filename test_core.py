import sqlite3
import unittest

import app
from app import fit_context
import chess_engine
import db


class CoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_connection = db._conn
        db._conn = sqlite3.connect(":memory:")
        db._conn.row_factory = sqlite3.Row
        db._persona_cache.clear()
        db.init_db()

    def tearDown(self) -> None:
        db._conn.close()
        db._conn = self.original_connection
        db._persona_cache.clear()

    def test_reply_chain_is_oldest_first(self) -> None:
        db.save_message(101, None, 1, "user", "first")
        db.save_message(102, 101, 1, "assistant", "second")

        chain = db.get_message_chain(102)

        self.assertEqual([row["content"] for row in chain], ["first", "second"])

    def test_context_fit_trims_old_history_and_warns(self) -> None:
        history = [
            {"role": "user", "content": "old prompt " * 800},
            {"role": "assistant", "content": "old answer " * 800},
        ]

        messages, warning = fit_context("persona", history, "latest question", 1024, 128)

        self.assertEqual(messages[-1]["content"], "latest question")
        self.assertTrue(warning)
        self.assertIn("trimmed", warning)
        self.assertLess(len(messages), len(history) + 2)

    def test_context_fit_warns_near_limit_without_trimming(self) -> None:
        history = [{"role": "user", "content": "brief context " * 300}]

        messages, warning = fit_context("system", history, "latest", 2048, 512)

        self.assertEqual(messages[-1]["content"], "latest")
        self.assertIsNotNone(warning)
        self.assertIn("near", warning)

    def test_chess_move_is_saved(self) -> None:
        ok, san, _fen = chess_engine.apply_user_move(1, "e4")

        self.assertTrue(ok)
        self.assertEqual(san, "e4")
        self.assertEqual(db.get_chess_game(1)["move_stack"], "e2e4")

    def test_channel_verbosity_is_persisted(self) -> None:
        self.assertIsNone(db.get_channel_verbosity(1))

        db.set_channel_verbosity(1, "detailed")

        self.assertEqual(db.get_channel_verbosity(1), "detailed")

    def test_slash_commands_are_registered(self) -> None:
        command_names = {command.name for command in app.bot.tree.get_commands()}

        self.assertTrue({"persona", "verbosity", "model", "cost", "status", "reset"} <= command_names)
        self.assertNotIn("personas", command_names)


if __name__ == "__main__":
    unittest.main()
