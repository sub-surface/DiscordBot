import sqlite3
import tempfile
import unittest
from pathlib import Path

from psychograph.store import ChannelSettings, Store


class StoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")

    def tearDown(self) -> None:
        self.store.close()

    def test_reply_chain_is_oldest_first(self) -> None:
        self.store.save_message(101, None, 1, "user", "first")
        self.store.save_message(102, 101, 1, "assistant", "second")

        self.assertEqual([row["content"] for row in self.store.message_chain(102, 1)], ["first", "second"])

    def test_reply_chain_does_not_cross_channel_boundaries(self) -> None:
        self.store.save_message(201, None, 1, "user", "private in channel one")
        self.store.save_message(202, 201, 2, "user", "reply in channel two")

        self.assertEqual([row["content"] for row in self.store.message_chain(202, 2)], ["reply in channel two"])

    def test_clear_channel_only_resets_that_channel_history(self) -> None:
        self.store.save_message(301, None, 1, "user", "channel one")
        self.store.save_message(302, None, 2, "user", "channel two")

        self.store.clear_channel(1)

        self.assertEqual(self.store.message_chain(301, 1), [])
        self.assertEqual([row["content"] for row in self.store.message_chain(302, 2)], ["channel two"])

    def test_channel_settings_default_and_persist_independently(self) -> None:
        self.assertEqual(self.store.channel_settings(1), ChannelSettings())

        self.store.update_channel(1, verbosity="detailed")
        self.store.update_channel(1, chess_commentary=True, persona_reactions=True)
        self.store.update_channel(1, persona="charlie")

        self.assertEqual(
            self.store.channel_settings(1),
            ChannelSettings(persona="charlie", verbosity="detailed", chess_commentary=True, persona_reactions=True),
        )
        self.assertEqual(self.store.channel_settings(2), ChannelSettings())

    def test_update_channel_rejects_unknown_settings_and_levels(self) -> None:
        with self.assertRaises(ValueError):
            self.store.update_channel(1, colour="blue")
        with self.assertRaises(ValueError):
            self.store.update_channel(1, verbosity="loud")

    def test_custom_personas_are_guild_scoped_and_case_insensitive(self) -> None:
        persona_id = self.store.create_custom_persona(10, 100, "Campfire", "Speak gently.")

        self.assertIsNotNone(persona_id)
        self.assertEqual(self.store.custom_persona(persona_id, 10)["prompt"], "Speak gently.")
        self.assertIsNone(self.store.custom_persona(persona_id, 11))
        self.assertIsNone(self.store.create_custom_persona(10, 101, "campfire", "Different voice."))
        self.assertIsNotNone(self.store.create_custom_persona(11, 101, "Campfire", "Another server."))

    def test_custom_persona_update_rejects_duplicate_names(self) -> None:
        first = self.store.create_custom_persona(10, 100, "Campfire", "Speak gently.")
        self.store.create_custom_persona(10, 100, "Lantern", "Glow.")

        self.assertFalse(self.store.update_custom_persona(first, 10, "lantern", "Glow too."))
        self.assertTrue(self.store.update_custom_persona(first, 10, "Ember", "Smoulder."))
        self.assertFalse(self.store.update_custom_persona(first, 11, "Ember", "Wrong server."))

    def test_deleting_custom_persona_moves_its_channels_to_fallback(self) -> None:
        persona_id = self.store.create_custom_persona(10, 100, "Campfire", "Speak gently.")
        self.store.update_channel(501, persona=f"custom:{persona_id}")
        self.store.update_channel(502, persona="charlie")

        self.assertTrue(self.store.delete_custom_persona(persona_id, 10, f"custom:{persona_id}", "mochi"))

        self.assertEqual(self.store.channel_settings(501).persona, "mochi")
        self.assertEqual(self.store.channel_settings(502).persona, "charlie")
        self.assertIsNone(self.store.custom_persona(persona_id, 10))
        self.assertFalse(self.store.delete_custom_persona(persona_id, 10, f"custom:{persona_id}", "mochi"))

    def test_opens_a_legacy_database_and_adds_missing_columns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.db"
            legacy = sqlite3.connect(path)
            legacy.execute(
                "CREATE TABLE messages (discord_msg_id INTEGER PRIMARY KEY, parent_msg_id INTEGER, "
                "channel_id INTEGER NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL, thinking TEXT)"
            )
            legacy.execute("CREATE TABLE channel_settings (channel_id INTEGER PRIMARY KEY, persona TEXT)")
            legacy.execute("INSERT INTO channel_settings VALUES (7, 'charlie')")
            legacy.commit()
            legacy.close()

            store = Store(path)
            try:
                self.assertEqual(store.channel_settings(7), ChannelSettings(persona="charlie"))
                store.save_message(1, None, 7, "user", "hello", author_id=3)
                self.assertEqual(store.message_chain(1, 7)[0]["author_id"], 3)
            finally:
                store.close()

    def test_chess_moves_round_trip(self) -> None:
        self.assertIsNone(self.store.chess_moves(1))

        self.store.save_chess_game(1, "fen", "e2e4 e7e5")

        self.assertEqual(self.store.chess_moves(1), "e2e4 e7e5")
        self.store.delete_chess_game(1)
        self.assertIsNone(self.store.chess_moves(1))


if __name__ == "__main__":
    unittest.main()
