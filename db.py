import sqlite3
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
        conn.commit()
