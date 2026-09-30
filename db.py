from __future__ import annotations

import os
import sqlite3
from pathlib import Path

DB_PATH = Path(os.getenv("DB_PATH", Path(__file__).parent / "history.db")).expanduser()
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row
_persona_cache: dict[int, str | None] = {}


def init_db() -> None:
    with _conn:
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS messages (
                discord_msg_id INTEGER PRIMARY KEY,
                parent_msg_id INTEGER,
                channel_id INTEGER NOT NULL,
                author_id INTEGER,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                thinking TEXT,
                ts DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        columns = {row["name"] for row in _conn.execute("PRAGMA table_info(messages)")}
        if "author_id" not in columns:
            _conn.execute("ALTER TABLE messages ADD COLUMN author_id INTEGER")
        if "thinking" not in columns:
            _conn.execute("ALTER TABLE messages ADD COLUMN thinking TEXT")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_channel ON messages(channel_id, discord_msg_id)")
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS channel_settings (
                channel_id INTEGER PRIMARY KEY,
                persona TEXT,
                verbosity TEXT,
                chess_commentary INTEGER NOT NULL DEFAULT 0
            )"""
        )
        settings_columns = {row["name"] for row in _conn.execute("PRAGMA table_info(channel_settings)")}
        if "verbosity" not in settings_columns:
            _conn.execute("ALTER TABLE channel_settings ADD COLUMN verbosity TEXT")
        if "chess_commentary" not in settings_columns:
            _conn.execute("ALTER TABLE channel_settings ADD COLUMN chess_commentary INTEGER NOT NULL DEFAULT 0")
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS chess_games (
                channel_id INTEGER PRIMARY KEY,
                fen TEXT NOT NULL,
                move_stack TEXT NOT NULL DEFAULT '',
                started_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        )


def save_message(
    discord_msg_id: int,
    parent_msg_id: int | None,
    channel_id: int,
    role: str,
    content: str,
    author_id: int | None = None,
) -> None:
    with _conn:
        _conn.execute(
            "INSERT OR REPLACE INTO messages "
            "(discord_msg_id, parent_msg_id, channel_id, author_id, role, content) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (discord_msg_id, parent_msg_id, channel_id, author_id, role, content),
        )


def get_message_chain(start_msg_id: int, limit: int = 40) -> list[dict]:
    rows = _conn.execute(
        """WITH RECURSIVE chain(discord_msg_id, parent_msg_id, author_id, role, content, depth) AS (
            SELECT discord_msg_id, parent_msg_id, author_id, role, content, 0
            FROM messages WHERE discord_msg_id = ?
            UNION ALL
            SELECT m.discord_msg_id, m.parent_msg_id, m.author_id, m.role, m.content, c.depth + 1
            FROM messages m JOIN chain c ON m.discord_msg_id = c.parent_msg_id
            WHERE c.depth < ?
        )
        SELECT discord_msg_id, author_id, role, content FROM chain ORDER BY depth DESC""",
        (start_msg_id, limit),
    ).fetchall()
    return [dict(row) for row in rows]


def clear_channel(channel_id: int) -> None:
    with _conn:
        _conn.execute("DELETE FROM messages WHERE channel_id = ?", (channel_id,))


def get_channel_persona(channel_id: int) -> str | None:
    if channel_id not in _persona_cache:
        row = _conn.execute(
            "SELECT persona FROM channel_settings WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        _persona_cache[channel_id] = row["persona"] if row else None
    return _persona_cache[channel_id]


def set_channel_persona(channel_id: int, persona: str) -> None:
    with _conn:
        _conn.execute(
            "INSERT INTO channel_settings (channel_id, persona) VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET persona = excluded.persona",
            (channel_id, persona),
        )
    _persona_cache[channel_id] = persona


def get_channel_verbosity(channel_id: int) -> str | None:
    row = _conn.execute(
        "SELECT verbosity FROM channel_settings WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    return row["verbosity"] if row else None


def set_channel_verbosity(channel_id: int, verbosity: str) -> None:
    with _conn:
        _conn.execute(
            "INSERT INTO channel_settings (channel_id, verbosity) VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET verbosity = excluded.verbosity",
            (channel_id, verbosity),
        )


def get_chess_commentary(channel_id: int) -> bool:
    row = _conn.execute(
        "SELECT chess_commentary FROM channel_settings WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    return bool(row["chess_commentary"]) if row else False


def set_chess_commentary(channel_id: int, enabled: bool) -> None:
    with _conn:
        _conn.execute(
            "INSERT INTO channel_settings (channel_id, chess_commentary) VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET chess_commentary = excluded.chess_commentary",
            (channel_id, int(enabled)),
        )


def save_chess_game(channel_id: int, fen: str, move_stack: str) -> None:
    with _conn:
        _conn.execute(
            "INSERT OR REPLACE INTO chess_games (channel_id, fen, move_stack, updated_ts) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (channel_id, fen, move_stack),
        )


def get_chess_game(channel_id: int) -> dict | None:
    row = _conn.execute(
        "SELECT channel_id, fen, move_stack FROM chess_games WHERE channel_id = ?", (channel_id,)
    ).fetchone()
    return dict(row) if row else None


def delete_chess_game(channel_id: int) -> None:
    with _conn:
        _conn.execute("DELETE FROM chess_games WHERE channel_id = ?", (channel_id,))
