"""
Chess game state manager.

Maintains one game per Discord channel, validates moves from both the user
and the LLM, and provides the authoritative FEN after every move.
Board state is persisted to SQLite so games survive restarts.
"""

from __future__ import annotations

import re
import logging
import chess

import db

log = logging.getLogger("bot")

# ── Helpers ──────────────────────────────────────────────────────────

_SAN_LIKE = re.compile(
    r"^(?:O-O(?:-O)?|0-0(?:-0)?|"                       # castling
    r"[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?)"  # normal SAN
    r"[+#]?$"
)
_UCI_LIKE = re.compile(r"^[a-h][1-8][a-h][1-8][qrbn]?$")


def _parse_move(board: chess.Board, text: str) -> chess.Move | None:
    """Try to parse *text* as a legal move on *board* (SAN or UCI)."""
    text = text.strip()
    # SAN first (e4, Nf3, O-O, Qxd5+, e8=Q …)
    try:
        return board.parse_san(text)
    except (chess.InvalidMoveError, chess.IllegalMoveError, chess.AmbiguousMoveError):
        pass
    # UCI fallback (e2e4, e7e8q …)
    try:
        move = chess.Move.from_uci(text)
        if move in board.legal_moves:
            return move
    except (chess.InvalidMoveError, ValueError):
        pass
    return None


def _move_stack_str(board: chess.Board) -> str:
    """Serialise the move stack as a space-separated UCI string."""
    return " ".join(m.uci() for m in board.move_stack)


def _board_from_moves(move_str: str) -> chess.Board:
    """Rebuild a board from a space-separated UCI move string."""
    board = chess.Board()
    if move_str:
        for uci in move_str.split():
            board.push_uci(uci)
    return board


# ── Public API ───────────────────────────────────────────────────────

def get_board(channel_id: int) -> chess.Board:
    """
    Load the game for *channel_id* from DB, or return a fresh starting
    position if no game exists yet.
    """
    row = db.get_chess_game(channel_id)
    if row and row["move_stack"]:
        try:
            return _board_from_moves(row["move_stack"])
        except Exception:
            log.warning("Corrupt move stack for channel %d — resetting", channel_id)
    return chess.Board()


def _save(channel_id: int, board: chess.Board) -> None:
    db.save_chess_game(channel_id, board.fen(), _move_stack_str(board))


def apply_user_move(channel_id: int, move_text: str) -> tuple[bool, str, str]:
    """
    Validate and apply the human's move.

    Returns (ok, message, fen):
      ok=True  → move applied, message is the SAN, fen is the new position
      ok=False → illegal, message is an error string, fen is the current position
    """
    board = get_board(channel_id)
    move = _parse_move(board, move_text)
    if move is None:
        legal = ", ".join(board.san(m) for m in board.legal_moves)
        return False, f"Illegal move: **{move_text}**. Legal moves: {legal}", board.fen()
    san = board.san(move)
    board.push(move)
    _save(channel_id, board)
    return True, san, board.fen()


def apply_bot_move(channel_id: int, move_text: str) -> tuple[bool, str, str]:
    """
    Validate and apply the LLM's move.

    Same return signature as apply_user_move.
    """
    board = get_board(channel_id)
    move = _parse_move(board, move_text)
    if move is None:
        legal = ", ".join(board.san(m) for m in board.legal_moves)
        return False, f"Illegal: {move_text}. Legal: {legal}", board.fen()
    san = board.san(move)
    board.push(move)
    _save(channel_id, board)
    return True, san, board.fen()


def extract_bot_move(text: str) -> str | None:
    """
    Pull the first chess-move-shaped token from the LLM's response text.
    Returns the raw token or None.
    """
    for token in text.split():
        token = token.strip(".,!?;:()\"'`*_~")
        if _SAN_LIKE.match(token) or _UCI_LIKE.match(token):
            return token
    return None


def legal_moves_str(channel_id: int) -> str:
    """Comma-separated SAN list of legal moves for the current position."""
    board = get_board(channel_id)
    return ", ".join(board.san(m) for m in board.legal_moves)


def game_status(channel_id: int) -> str | None:
    """
    Return a human-readable game-over string, or None if the game is ongoing.
    """
    board = get_board(channel_id)
    if board.is_checkmate():
        winner = "Black" if board.turn == chess.WHITE else "White"
        return f"Checkmate — {winner} wins."
    if board.is_stalemate():
        return "Stalemate — draw."
    if board.is_insufficient_material():
        return "Draw — insufficient material."
    if board.is_fifty_moves():
        return "Draw — fifty-move rule."
    if board.is_repetition(3):
        return "Draw — threefold repetition."
    return None


def current_fen(channel_id: int) -> str:
    return get_board(channel_id).fen()


def reset_game(channel_id: int) -> None:
    db.delete_chess_game(channel_id)


def is_chess_persona(persona_name: str) -> bool:
    """True if the active persona is the chess persona."""
    return persona_name.lower() == "chess"


def move_number(channel_id: int) -> int:
    """Current full-move number."""
    return get_board(channel_id).fullmove_number


def side_to_move(channel_id: int) -> str:
    board = get_board(channel_id)
    return "White" if board.turn == chess.WHITE else "Black"
