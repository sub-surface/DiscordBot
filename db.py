import sqlite3
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "history.db"


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        # Migrate old schema (per-channel rolling history) to new reply-chain schema
        cols = [r[1] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
        if cols and "discord_msg_id" not in cols:
            conn.execute("DROP TABLE messages")
            conn.execute("DROP INDEX IF EXISTS idx_channel")
            conn.commit()

        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                discord_msg_id  INTEGER PRIMARY KEY,
                parent_msg_id   INTEGER,
                channel_id      INTEGER NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                ts              DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_channel ON messages(channel_id, discord_msg_id)"
        )
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pins (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id  INTEGER NOT NULL,
                content     TEXT NOT NULL,
                ts          DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS chess_games (
                channel_id  INTEGER PRIMARY KEY,
                fen         TEXT NOT NULL,
                move_stack  TEXT NOT NULL DEFAULT '',
                started_ts  DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_ts  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_settings (
                channel_id  INTEGER PRIMARY KEY,
                persona     TEXT,
                verbosity   INTEGER NOT NULL DEFAULT 2,
                reset_ts    REAL
            )
        """)
        # Migration: add reset_ts column to existing tables that predate it
        cs_cols = [r[1] for r in conn.execute("PRAGMA table_info(channel_settings)").fetchall()]
        if "reset_ts" not in cs_cols:
            conn.execute("ALTER TABLE channel_settings ADD COLUMN reset_ts REAL")
        conn.commit()


def save_message(
    discord_msg_id: int,
    parent_msg_id: int | None,
    channel_id: int,
    role: str,
    content: str,
) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO messages "
            "(discord_msg_id, parent_msg_id, channel_id, role, content) "
            "VALUES (?, ?, ?, ?, ?)",
            (discord_msg_id, parent_msg_id, channel_id, role, content),
        )
        conn.commit()


def get_message(discord_msg_id: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT discord_msg_id, parent_msg_id, channel_id, role, content "
            "FROM messages WHERE discord_msg_id = ?",
            (discord_msg_id,),
        ).fetchone()
    if row:
        return {
            "discord_msg_id": row[0],
            "parent_msg_id": row[1],
            "channel_id": row[2],
            "role": row[3],
            "content": row[4],
        }
    return None


def delete_message(discord_msg_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM messages WHERE discord_msg_id = ?", (discord_msg_id,))
        conn.commit()


def add_pin(channel_id: int, content: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO pins (channel_id, content) VALUES (?, ?)",
            (channel_id, content[:200]),
        )
        conn.commit()


def get_pins(channel_id: int) -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT content FROM pins WHERE channel_id = ? ORDER BY ts DESC LIMIT 5",
            (channel_id,),
        ).fetchall()
    return [row[0] for row in rows]


def clear_channel(channel_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM messages WHERE channel_id = ?", (channel_id,))
        # Record the reset timestamp so build_context can ignore pre-reset Discord messages
        conn.execute(
            "INSERT INTO channel_settings (channel_id, reset_ts) VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET reset_ts = excluded.reset_ts",
            (channel_id, time.time()),
        )
        conn.commit()


def get_channel_reset_ts(channel_id: int) -> float | None:
    """Return the Unix timestamp of the last reset, or None if never reset."""
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT reset_ts FROM channel_settings WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
    return row[0] if row else None


# ── Chess game persistence ──────────────────────────────────────────

def save_chess_game(channel_id: int, fen: str, move_stack: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO chess_games "
            "(channel_id, fen, move_stack, updated_ts) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (channel_id, fen, move_stack),
        )
        conn.commit()


def get_chess_game(channel_id: int) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT channel_id, fen, move_stack FROM chess_games WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
    if row:
        return {"channel_id": row[0], "fen": row[1], "move_stack": row[2]}
    return None


def delete_chess_game(channel_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM chess_games WHERE channel_id = ?", (channel_id,))
        conn.commit()


# ── Per-channel settings ───────────────────────────────────────────────────────

def get_channel_persona(channel_id: int) -> str | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT persona FROM channel_settings WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
    return row[0] if row else None


def set_channel_persona(channel_id: int, persona: str) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO channel_settings (channel_id, persona) VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET persona = excluded.persona",
            (channel_id, persona),
        )
        conn.commit()


def get_channel_verbosity(channel_id: int) -> int:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT verbosity FROM channel_settings WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()
    return row[0] if row else 2


def set_channel_verbosity(channel_id: int, verbosity: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO channel_settings (channel_id, verbosity) VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET verbosity = excluded.verbosity",
            (channel_id, verbosity),
        )
        conn.commit()
