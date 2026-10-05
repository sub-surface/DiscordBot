"""Chess games, one per channel: the human plays White, local Stockfish plays Black."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import threading
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

import chess

from .backends import Backend
from .settings import ROOT, Settings
from .store import Store

log = logging.getLogger("psychograph.chess")

COMMENTARY_PROMPT = (
    "You are commenting on a completed chess turn. Do not suggest or invent moves. "
    "Give one brief, accurate observation about the position or last move."
)


def find_stockfish(configured: str) -> str | None:
    """STOCKFISH_PATH (absolute, relative to the project, or on PATH), else `stockfish` on PATH."""
    configured = configured.strip()
    if not configured:
        return shutil.which("stockfish") or shutil.which("stockfish.exe")
    found = shutil.which(configured)
    if found:
        return found
    candidate = Path(os.path.expandvars(os.path.expanduser(configured)))
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    return str(candidate) if candidate.is_file() else None


class Stockfish:
    """A persistent Stockfish process spoken to over UCI.

    Blocking by design and called through `asyncio.to_thread`: on Windows, importing
    modal switches asyncio to a loop without subprocess support, so the engine can't
    rely on asyncio subprocesses.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()

    def best_move(self, board: chess.Board) -> chess.Move:
        move_ms = int(max(0.05, self.settings.stockfish_move_time) * 1000)
        with self._lock:
            process = self._ensure_started()
            watchdog = threading.Timer(move_ms / 1000 + 20, process.kill)
            watchdog.start()
            try:
                self._send(f"position fen {board.fen()}")
                self._send(f"go movetime {move_ms}")
                reply = self._read_until("bestmove")
            except Exception:
                self._stop()
                raise
            finally:
                watchdog.cancel()
        move = chess.Move.from_uci(reply.split()[1])
        if move not in board.legal_moves:
            raise RuntimeError(f"Stockfish returned an illegal move: {move.uci()}")
        return move

    def close(self) -> None:
        with self._lock:
            self._stop()

    def _ensure_started(self) -> subprocess.Popen[str]:
        if self._process is not None and self._process.poll() is None:
            return self._process
        executable = find_stockfish(self.settings.stockfish_path)
        if executable is None:
            raise FileNotFoundError("Stockfish was not found. Install Stockfish and set STOCKFISH_PATH to its executable.")
        self._process = subprocess.Popen(
            [executable],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        try:
            self._send("uci")
            self._read_until("uciok")
            self._send(f"setoption name Threads value {max(1, self.settings.stockfish_threads)}")
            self._send(f"setoption name Hash value {max(16, self.settings.stockfish_hash_mb)}")
            self._send("isready")
            self._read_until("readyok")
        except Exception:
            self._stop()
            raise
        return self._process

    def _send(self, command: str) -> None:
        assert self._process and self._process.stdin
        self._process.stdin.write(command + "\n")
        self._process.stdin.flush()

    def _read_until(self, prefix: str) -> str:
        assert self._process and self._process.stdout
        for line in self._process.stdout:
            if line.startswith(prefix):
                return line.strip()
        raise RuntimeError("Stockfish exited unexpectedly.")

    def _stop(self) -> None:
        if self._process is None:
            return
        if self._process.poll() is None:
            try:
                self._send("quit")
                self._process.wait(timeout=2)
            except (OSError, ValueError, subprocess.TimeoutExpired):
                self._process.kill()
        self._process = None


def game_status(board: chess.Board) -> str | None:
    """A human-readable game-over string, or None while the game is ongoing."""
    if board.is_checkmate():
        return f"Checkmate — {'Black' if board.turn == chess.WHITE else 'White'} wins."
    if board.is_stalemate():
        return "Stalemate — draw."
    if board.is_insufficient_material():
        return "Draw — insufficient material."
    if board.is_fifty_moves():
        return "Draw — fifty-move rule."
    if board.is_repetition(3):
        return "Draw — threefold repetition."
    return None


def parse_move(board: chess.Board, text: str) -> chess.Move | None:
    """A legal move from SAN (e4, Nf3, O-O) or UCI (e2e4), or None."""
    text = text.strip()
    try:
        return board.parse_san(text)
    except ValueError:
        pass
    try:
        move = chess.Move.from_uci(text)
    except ValueError:
        return None
    return move if move in board.legal_moves else None


@dataclass(frozen=True)
class Turn:
    summary: str
    fen: str | None   # the position to draw, or None when nothing changed worth showing


class ChessService:
    def __init__(self, store: Store, engine: Stockfish, commentator: Backend | None = None) -> None:
        self.store = store
        self.engine = engine
        self.commentator = commentator
        self._locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

    def board(self, channel_id: int) -> chess.Board:
        board = chess.Board()
        for uci in (self.store.chess_moves(channel_id) or "").split():
            try:
                board.push_uci(uci)
            except ValueError:
                log.warning("Corrupt move stack for channel %d — resetting", channel_id)
                return chess.Board()
        return board

    def reset(self, channel_id: int) -> None:
        self.store.delete_chess_game(channel_id)

    def _save(self, channel_id: int, board: chess.Board) -> None:
        self.store.save_chess_game(channel_id, board.fen(), " ".join(move.uci() for move in board.move_stack))

    async def play(self, channel_id: int, move_text: str) -> Turn:
        """Apply the human's move and Stockfish's reply. Nothing is saved unless both succeed."""
        async with self._locks[channel_id]:
            board = self.board(channel_id)
            original_fen = board.fen()
            move = parse_move(board, move_text)
            if move is None:
                legal = ", ".join(board.san(legal_move) for legal_move in board.legal_moves)
                return Turn(f"Illegal move: **{move_text}**. Legal moves: {legal}", None)
            san = board.san(move)
            board.push(move)

            status = game_status(board)
            if status:
                self._save(channel_id, board)
                return Turn(status, board.fen())

            try:
                reply = await asyncio.to_thread(self.engine.best_move, board.copy())
            except Exception:
                log.exception("CPU chess engine failed in channel %s", channel_id)
                return Turn(
                    "I couldn't get a move from the local chess engine, so I rolled back yours. "
                    "Check that Stockfish is installed and STOCKFISH_PATH is set.",
                    original_fen,
                )
            reply_san = board.san(reply)
            board.push(reply)
            self._save(channel_id, board)

        summary = f"**{san}**  ·  **{reply_san}**"
        status = game_status(board)
        if status:
            summary += f"\n{status}"
        elif self.store.channel_settings(channel_id).chess_commentary:
            commentary = await self._commentary(channel_id, san, reply_san, board.fen())
            if commentary:
                summary += f"\n{commentary[:500]}"
        return Turn(summary, board.fen())

    async def _commentary(self, channel_id: int, san: str, reply_san: str, fen: str) -> str | None:
        if self.commentator is None:
            return None
        messages = [
            {"role": "system", "content": COMMENTARY_PROMPT},
            {"role": "user", "content": f"The moves were {san} and {reply_san}. Current position: {fen}."},
        ]
        try:
            return (await self.commentator.complete(messages)).text
        except Exception:
            log.exception("Optional local chess commentary failed in channel %s", channel_id)
            return None
