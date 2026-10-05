import asyncio
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import chess

from psychograph.backends import Completion
from psychograph.chess_game import ChessService, Stockfish, find_stockfish, parse_move
from psychograph.settings import load_settings
from psychograph.store import Store


def engine_playing(*replies: str, delay: float = 0.0) -> MagicMock:
    engine = MagicMock()
    moves = iter(replies)

    def best_move(board: chess.Board) -> chess.Move:
        time.sleep(delay)
        return chess.Move.from_uci(next(moves))

    engine.best_move.side_effect = best_move
    return engine


class ChessServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Store(":memory:")
        self.commentator = MagicMock(complete=AsyncMock(return_value=Completion("Symmetrical.")))

    def tearDown(self) -> None:
        self.store.close()

    def service(self, engine: MagicMock) -> ChessService:
        return ChessService(self.store, engine, self.commentator)

    def test_turn_plays_both_moves_with_the_cpu_engine_only(self) -> None:
        turn = asyncio.run(self.service(engine_playing("e7e5")).play(1, "e4"))

        self.assertIn("**e4**", turn.summary)
        self.assertIn("**e5**", turn.summary)
        self.assertEqual(self.store.chess_moves(1), "e2e4 e7e5")
        self.commentator.complete.assert_not_awaited()

    def test_engine_failure_saves_nothing(self) -> None:
        engine = MagicMock()
        engine.best_move.side_effect = FileNotFoundError("Stockfish missing")
        service = self.service(engine)

        with patch("psychograph.chess_game.log.exception"):
            turn = asyncio.run(service.play(1, "e4"))

        self.assertIn("rolled back", turn.summary)
        self.assertEqual(turn.fen, chess.Board().fen())
        self.assertIsNone(self.store.chess_moves(1))

    def test_illegal_move_lists_legal_moves_without_a_board(self) -> None:
        engine = engine_playing()

        turn = asyncio.run(self.service(engine).play(1, "e5"))

        self.assertIn("Illegal move", turn.summary)
        self.assertIn("Nf3", turn.summary)
        self.assertIsNone(turn.fen)
        engine.best_move.assert_not_called()

    def test_commentary_is_added_when_enabled(self) -> None:
        self.store.update_channel(1, chess_commentary=True)

        turn = asyncio.run(self.service(engine_playing("e7e5")).play(1, "e4"))

        self.assertIn("Symmetrical.", turn.summary)

    def test_human_checkmate_ends_the_game_without_asking_the_engine(self) -> None:
        self.store.save_chess_game(1, "", "e2e4 f7f6 d2d4 g7g5")
        engine = engine_playing()

        turn = asyncio.run(self.service(engine).play(1, "Qh5#"))

        self.assertIn("Checkmate — White wins.", turn.summary)
        engine.best_move.assert_not_called()
        self.assertTrue(self.store.chess_moves(1).endswith("d1h5"))

    def test_concurrent_moves_in_one_channel_are_serialised(self) -> None:
        service = self.service(engine_playing("e7e5", "b8c6", delay=0.05))

        async def both() -> list:
            return await asyncio.gather(service.play(1, "e4"), service.play(1, "Nf3"))

        first, second = asyncio.run(both())

        self.assertIn("**e5**", first.summary)
        self.assertIn("**Nc6**", second.summary)
        self.assertEqual(self.store.chess_moves(1), "e2e4 e7e5 g1f3 b8c6")

    def test_parse_move_accepts_san_and_uci(self) -> None:
        board = chess.Board()

        self.assertEqual(parse_move(board, "Nf3"), chess.Move.from_uci("g1f3"))
        self.assertEqual(parse_move(board, "e2e4"), chess.Move.from_uci("e2e4"))
        self.assertIsNone(parse_move(board, "e2e5"))
        self.assertIsNone(parse_move(board, "hello"))


@unittest.skipUnless(find_stockfish(load_settings().stockfish_path), "Stockfish is not installed")
class StockfishIntegrationTests(unittest.TestCase):
    def test_real_engine_returns_legal_moves_and_survives_restart(self) -> None:
        settings = load_settings()
        engine = Stockfish(settings)
        try:
            board = chess.Board()
            first = engine.best_move(board)
            self.assertIn(first, board.legal_moves)

            engine.close()  # the next call starts a fresh process
            board.push(first)
            second = engine.best_move(board)
            self.assertIn(second, board.legal_moves)
        finally:
            engine.close()

    def test_real_engine_works_on_a_selector_event_loop(self) -> None:
        # Importing modal on Windows forces this loop type, which lacks asyncio subprocess support.
        engine = Stockfish(load_settings())
        loop = asyncio.SelectorEventLoop()
        try:
            store = Store(":memory:")
            turn = loop.run_until_complete(ChessService(store, engine).play(1, "d4"))
            self.assertIn("**d4**", turn.summary)
            self.assertEqual(len(store.chess_moves(1).split()), 2)
            store.close()
        finally:
            loop.close()
            engine.close()


if __name__ == "__main__":
    unittest.main()
