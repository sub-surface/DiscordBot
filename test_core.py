import asyncio
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest

import chess
import discord
import app
import chess_game
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

    def test_chess_uses_cpu_engine_without_language_model(self) -> None:
        bot = SimpleNamespace(
            generate_local=AsyncMock(side_effect=AssertionError("commentary is disabled by default")),
            generate=AsyncMock(side_effect=AssertionError("chess must not use the configured backend")),
        )
        channel = SimpleNamespace(id=1)

        with patch("chess_game._board_file", return_value=None), patch(
            "chess_game.chess_engine.find_cpu_move", return_value="e5"
        ) as find_cpu_move:
            summary, _file = asyncio.run(chess_game.play_move(bot, channel, "e4"))

        find_cpu_move.assert_called_once_with(1)
        bot.generate_local.assert_not_awaited()
        bot.generate.assert_not_awaited()
        self.assertIn("e4", summary)
        self.assertIn("e5", summary)
        self.assertEqual(db.get_chess_game(1)["move_stack"], "e2e4 e7e5")

    def test_chess_rolls_back_when_stockfish_is_missing(self) -> None:
        bot = SimpleNamespace(generate_local=AsyncMock(), generate=AsyncMock())
        channel = SimpleNamespace(id=1)

        with patch("chess_game._board_file", return_value=None), patch(
            "chess_game.chess_engine.find_cpu_move", side_effect=FileNotFoundError("Stockfish missing")
        ), patch("chess_game.log.exception"):
            summary, _file = asyncio.run(chess_game.play_move(bot, channel, "e4"))

        self.assertIn("rolled back", summary)
        self.assertEqual(chess_engine.current_fen(1), chess.Board().fen())
        bot.generate_local.assert_not_awaited()
        bot.generate.assert_not_awaited()

    def test_chess_commentary_setting_defaults_off_and_persists(self) -> None:
        self.assertFalse(db.get_chess_commentary(1))

        db.set_chess_commentary(1, True)

        self.assertTrue(db.get_chess_commentary(1))

    def test_channel_verbosity_is_persisted(self) -> None:
        self.assertIsNone(db.get_channel_verbosity(1))

        db.set_channel_verbosity(1, "detailed")

        self.assertEqual(db.get_channel_verbosity(1), "detailed")

    def test_slash_commands_are_registered(self) -> None:
        command_names = {command.name for command in app.bot.tree.get_commands()}

        self.assertTrue({"persona", "verbosity", "model", "cost", "status", "reset"} <= command_names)
        self.assertNotIn("personas", command_names)

    def test_channel_allowlist(self) -> None:
        self.assertTrue(app.is_allowed_channel(SimpleNamespace(name="sim-city", parent=None)))
        self.assertTrue(app.is_allowed_channel(SimpleNamespace(name="little-st-james", parent=None)))
        self.assertTrue(app.is_allowed_channel(SimpleNamespace(name="chess", parent=None)))
        self.assertFalse(app.is_allowed_channel(SimpleNamespace(name="general", parent=None)))
        self.assertFalse(app.is_allowed_channel(None))

    def test_thread_uses_parent_channel_allowlist(self) -> None:
        parent = SimpleNamespace(name="chess")
        thread = SimpleNamespace(name="game-thread", parent=parent)

        self.assertTrue(app.is_allowed_channel(thread))

    def test_command_tree_blocks_channels_outside_allowlist(self) -> None:
        response = SimpleNamespace(send_message=AsyncMock(), autocomplete=AsyncMock())
        interaction = SimpleNamespace(
            channel=SimpleNamespace(name="general", parent=None),
            guild=SimpleNamespace(id=1),
            type=discord.InteractionType.application_command,
            response=response,
        )

        allowed = asyncio.run(app.bot.tree.interaction_check(interaction))

        self.assertFalse(allowed)
        response.send_message.assert_awaited_once()
        self.assertTrue(response.send_message.await_args.kwargs["ephemeral"])

    def test_command_tree_hides_autocomplete_outside_allowlist(self) -> None:
        response = SimpleNamespace(send_message=AsyncMock(), autocomplete=AsyncMock())
        interaction = SimpleNamespace(
            channel=SimpleNamespace(name="general", parent=None),
            guild=SimpleNamespace(id=1),
            type=discord.InteractionType.autocomplete,
            response=response,
        )

        allowed = asyncio.run(app.bot.tree.interaction_check(interaction))

        self.assertFalse(allowed)
        response.autocomplete.assert_awaited_once_with([])
        response.send_message.assert_not_awaited()

    def test_command_tree_silently_blocks_dms(self) -> None:
        response = SimpleNamespace(send_message=AsyncMock(), autocomplete=AsyncMock())
        interaction = SimpleNamespace(
            channel=SimpleNamespace(name="DM User", parent=None),
            guild=None,
            type=discord.InteractionType.application_command,
            response=response,
        )

        allowed = asyncio.run(app.bot.tree.interaction_check(interaction))

        self.assertFalse(allowed)
        response.send_message.assert_not_awaited()

    def test_deleted_failure_placeholder_does_not_raise(self) -> None:
        response = SimpleNamespace(status=404, reason="Not Found", headers={})
        error = discord.NotFound(response, {"code": 10008, "message": "Unknown Message"})
        placeholder = SimpleNamespace(edit=AsyncMock(side_effect=error))

        with patch("app.log.info"):
            asyncio.run(app._show_generation_failure(placeholder, 1))

        placeholder.edit.assert_awaited_once()

    def test_chess_commentary_subcommand_is_registered(self) -> None:
        chess_group = next(
            command
            for command in chess_game.ChessCommands.__cog_app_commands__
            if command.name == "chess"
        )

        self.assertIn("commentary", {command.name for command in chess_group.commands})

    def test_startup_clears_guild_commands_once(self) -> None:
        guild = SimpleNamespace(name="test", id=1)
        tree = SimpleNamespace(sync=AsyncMock())
        bot = SimpleNamespace(
            _legacy_guild_commands_cleared=False,
            guilds=[guild],
            tree=tree,
        )

        asyncio.run(app.PsychographBot.on_ready(bot))
        asyncio.run(app.PsychographBot.on_ready(bot))

        tree.sync.assert_awaited_once_with(guild=guild)
        self.assertTrue(bot._legacy_guild_commands_cleared)


if __name__ == "__main__":
    unittest.main()
