import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager

DB_PATH = Path(__file__).parent / "history.db"

# Persistent connection for the lifetime of the process
_conn = sqlite3.connect(DB_PATH, check_same_thread=False)
_conn.row_factory = sqlite3.Row

def init_db() -> None:
    with _conn:
        # Migration: ensure messages table is up to date
        cols = [r["name"] for r in _conn.execute("PRAGMA table_info(messages)").fetchall()]
        if cols and "discord_msg_id" not in cols:
            _conn.execute("DROP TABLE messages")
            _conn.execute("DROP INDEX IF EXISTS idx_channel")

        _conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                discord_msg_id  INTEGER PRIMARY KEY,
                parent_msg_id   INTEGER,
                channel_id      INTEGER NOT NULL,
                role            TEXT NOT NULL,
                content         TEXT NOT NULL,
                ts              DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_channel ON messages(channel_id, discord_msg_id)")
        
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS pins (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id  INTEGER NOT NULL,
                content     TEXT NOT NULL,
                ts              DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS chess_games (
                channel_id  INTEGER PRIMARY KEY,
                fen         TEXT NOT NULL,
                move_stack  TEXT NOT NULL DEFAULT '',
                started_ts  DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_ts  DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        
        _conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_settings (
                channel_id  INTEGER PRIMARY KEY,
                persona     TEXT,
                verbosity   INTEGER NOT NULL DEFAULT 2,
                reset_ts    REAL,
                temperature REAL
            )
        """)

        _conn.execute("""
            CREATE TABLE IF NOT EXISTS usage_logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_msg_id  INTEGER,
                model           TEXT,
                provider        TEXT,
                prompt_tokens   INTEGER,
                completion_tokens INTEGER,
                total_time      REAL,
                ts              DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)

        _conn.execute("""
            CREATE TABLE IF NOT EXISTS channel_webhooks (
                channel_id      INTEGER PRIMARY KEY,
                webhook_url     TEXT NOT NULL,
                webhook_id      INTEGER
            )
        """)

        _conn.execute("""
            CREATE TABLE IF NOT EXISTS heartbeats (
                task_name       TEXT PRIMARY KEY,
                last_run_ts     REAL
            )
        """)
        
        # Migration: add columns to existing tables that predate them
        cs_cols = [r["name"] for r in _conn.execute("PRAGMA table_info(channel_settings)").fetchall()]
        if "reset_ts" not in cs_cols:
            _conn.execute("ALTER TABLE channel_settings ADD COLUMN reset_ts REAL")
        if "temperature" not in cs_cols:
            _conn.execute("ALTER TABLE channel_settings ADD COLUMN temperature REAL")

def save_message(discord_msg_id: int, parent_msg_id: int | None, channel_id: int, role: str, content: str) -> None:
    with _conn:
        _conn.execute(
            "INSERT OR REPLACE INTO messages (discord_msg_id, parent_msg_id, channel_id, role, content) VALUES (?, ?, ?, ?, ?)",
            (discord_msg_id, parent_msg_id, channel_id, role, content),
        )

def get_message(discord_msg_id: int) -> dict | None:
    row = _conn.execute(
        "SELECT discord_msg_id, parent_msg_id, channel_id, role, content FROM messages WHERE discord_msg_id = ?",
        (discord_msg_id,),
    ).fetchone()
    return dict(row) if row else None

def delete_message(discord_msg_id: int) -> None:
    with _conn:
        _conn.execute("DELETE FROM messages WHERE discord_msg_id = ?", (discord_msg_id,))

def get_message_chain(start_msg_id: int, limit: int = 40) -> list[dict]:
    """Fetch a chain of parent messages using a Recursive CTE."""
    query = """
    WITH RECURSIVE chain(discord_msg_id, parent_msg_id, role, content, depth) AS (
        SELECT discord_msg_id, parent_msg_id, role, content, 0
        FROM messages 
        WHERE discord_msg_id = ?
        UNION ALL
        SELECT m.discord_msg_id, m.parent_msg_id, m.role, m.content, c.depth + 1
        FROM messages m
        JOIN chain c ON m.discord_msg_id = c.parent_msg_id
        WHERE c.depth < ?
    )
    SELECT role, content FROM chain ORDER BY depth DESC;
    """
    rows = _conn.execute(query, (start_msg_id, limit)).fetchall()
    return [{"role": r["role"], "content": r["content"]} for r in rows]

def add_pin(channel_id: int, content: str) -> None:
    with _conn:
        _conn.execute("INSERT INTO pins (channel_id, content) VALUES (?, ?)", (channel_id, content[:200]))

def get_pins(channel_id: int) -> list[str]:
    rows = _conn.execute(
        "SELECT content FROM pins WHERE channel_id = ? ORDER BY ts DESC LIMIT 5",
        (channel_id,),
    ).fetchall()
    return [row["content"] for row in rows]

def clear_channel(channel_id: int) -> None:
    with _conn:
        _conn.execute("DELETE FROM messages WHERE channel_id = ?", (channel_id,))
        _conn.execute(
            "INSERT INTO channel_settings (channel_id, reset_ts) VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET reset_ts = excluded.reset_ts",
            (channel_id, time.time()),
        )

def get_channel_reset_ts(channel_id: int) -> float | None:
    row = _conn.execute(
        "SELECT reset_ts FROM channel_settings WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    return row["reset_ts"] if row else None

# ── Chess game persistence ──────────────────────────────────────────

def save_chess_game(channel_id: int, fen: str, move_stack: str) -> None:
    with _conn:
        _conn.execute(
            "INSERT OR REPLACE INTO chess_games (channel_id, fen, move_stack, updated_ts) "
            "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (channel_id, fen, move_stack),
        )

def get_chess_game(channel_id: int) -> dict | None:
    row = _conn.execute(
        "SELECT channel_id, fen, move_stack FROM chess_games WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    return dict(row) if row else None

def delete_chess_game(channel_id: int) -> None:
    with _conn:
        _conn.execute("DELETE FROM chess_games WHERE channel_id = ?", (channel_id,))

# ── Per-channel settings ───────────────────────────────────────────────────────

_CHANNEL_CACHE = {}

def get_channel_persona(channel_id: int) -> str | None:
    if channel_id in _CHANNEL_CACHE and "persona" in _CHANNEL_CACHE[channel_id]:
        return _CHANNEL_CACHE[channel_id]["persona"]
        
    row = _conn.execute(
        "SELECT persona FROM channel_settings WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    
    p = row[0] if row else None
    _CHANNEL_CACHE.setdefault(channel_id, {})["persona"] = p
    return p

def set_channel_persona(channel_id: int, persona: str) -> None:
    with _conn:
        _conn.execute(
            "INSERT INTO channel_settings (channel_id, persona) VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET persona = excluded.persona",
            (channel_id, persona),
        )
    _CHANNEL_CACHE.setdefault(channel_id, {})["persona"] = persona

def get_channel_verbosity(channel_id: int) -> int:
    if channel_id in _CHANNEL_CACHE and "verbosity" in _CHANNEL_CACHE[channel_id]:
        return _CHANNEL_CACHE[channel_id]["verbosity"]
        
    row = _conn.execute(
        "SELECT verbosity FROM channel_settings WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    
    v = row["verbosity"] if row else 2
    _CHANNEL_CACHE.setdefault(channel_id, {})["verbosity"] = v
    return v

def set_channel_verbosity(channel_id: int, verbosity: int) -> None:
    with _conn:
        _conn.execute(
            "INSERT INTO channel_settings (channel_id, verbosity) VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET verbosity = excluded.verbosity",
            (channel_id, verbosity),
        )
    _CHANNEL_CACHE.setdefault(channel_id, {})["verbosity"] = verbosity

def get_channel_temperature(channel_id: int) -> float | None:
    if channel_id in _CHANNEL_CACHE and "temp" in _CHANNEL_CACHE[channel_id]:
        return _CHANNEL_CACHE[channel_id]["temp"]
        
    row = _conn.execute(
        "SELECT temperature FROM channel_settings WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    
    t = row["temperature"] if row else None
    _CHANNEL_CACHE.setdefault(channel_id, {})["temp"] = t
    return t

def set_channel_temperature(channel_id: int, temperature: float) -> None:
    with _conn:
        _conn.execute(
            "INSERT INTO channel_settings (channel_id, temperature) VALUES (?, ?) "
            "ON CONFLICT(channel_id) DO UPDATE SET temperature = excluded.temperature",
            (channel_id, temperature),
        )
    _CHANNEL_CACHE.setdefault(channel_id, {})["temp"] = temperature

# ── Webhook persistence ──────────────────────────────────────────────

def save_channel_webhook(channel_id: int, webhook_url: str, webhook_id: int | None = None) -> None:
    with _conn:
        _conn.execute(
            "INSERT OR REPLACE INTO channel_webhooks (channel_id, webhook_url, webhook_id) VALUES (?, ?, ?)",
            (channel_id, webhook_url, webhook_id),
        )

def get_channel_webhook(channel_id: int) -> dict | None:
    row = _conn.execute(
        "SELECT webhook_url, webhook_id FROM channel_webhooks WHERE channel_id = ?",
        (channel_id,),
    ).fetchone()
    return dict(row) if row else None

# ── Heartbeat tracking ─────────────────────────────────────────────

def get_last_run(task_name: str) -> float:
    row = _conn.execute("SELECT last_run_ts FROM heartbeats WHERE task_name = ?", (task_name,)).fetchone()
    return row[0] if row else 0.0

def set_last_run(task_name: str, ts: float) -> None:
    with _conn:
        _conn.execute(
            "INSERT INTO heartbeats (task_name, last_run_ts) VALUES (?, ?) "
            "ON CONFLICT(task_name) DO UPDATE SET last_run_ts = excluded.last_run_ts",
            (task_name, ts),
        )

# ── Usage Logging ─────────────────────────────────────────────────────────────

def log_usage(msg_id: int, model: str, provider: str, prompt_tok: int, comp_tok: int, duration: float) -> None:
    with _conn:
        _conn.execute(
            "INSERT INTO usage_logs (discord_msg_id, model, provider, prompt_tokens, completion_tokens, total_time) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (msg_id, model, provider, prompt_tok, comp_tok, duration),
        )

def get_latest_usage(msg_id: int) -> dict | None:
    row = _conn.execute(
        "SELECT model, provider, prompt_tokens, completion_tokens, total_time FROM usage_logs "
        "WHERE discord_msg_id = ? ORDER BY ts DESC LIMIT 1",
        (msg_id,),
    ).fetchone()
    return dict(row) if row else None
