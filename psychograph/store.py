"""SQLite persistence: reply-chain history, channel settings, custom personas, chess games."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from pathlib import Path

VERBOSITY_LEVELS = ("concise", "balanced", "detailed")


@dataclass(frozen=True)
class ChannelSettings:
    persona: str | None = None
    verbosity: str = "balanced"
    chess_commentary: bool = False
    persona_reactions: bool = False
    persona_voice: bool = False      # speak through a webhook as the persona
    sounds: bool = False             # let personas play soundbank clips


SETTING_COLUMNS = {
    "persona": "TEXT",
    "verbosity": "TEXT",
    "chess_commentary": "INTEGER NOT NULL DEFAULT 0",
    "persona_reactions": "INTEGER NOT NULL DEFAULT 0",
    "persona_voice": "INTEGER NOT NULL DEFAULT 0",
    "sounds": "INTEGER NOT NULL DEFAULT 0",
}


@dataclass(frozen=True)
class Generation:
    channel_id: int
    guild_id: int | None
    persona: str
    model: str
    profile: str
    ok: bool
    wall_seconds: float
    completion_tokens: int | None = None
    prompt_tokens: int | None = None
    tokens_per_second: float | None = None
    cold_start: bool = False
    trimmed: bool = False
    error: str | None = None


class Store:
    def __init__(self, path: str | Path = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._migrate()

    def close(self) -> None:
        self._conn.close()

    def _migrate(self) -> None:
        with self._conn:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS messages (
                    discord_msg_id INTEGER PRIMARY KEY,
                    parent_msg_id INTEGER,
                    channel_id INTEGER NOT NULL,
                    author_id INTEGER,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    ts DATETIME DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            # For assistant rows: reply_to is the user message answered, requester_id who asked for the
            # answer (not always that message's author), and answer_id the answer's first message. One
            # request can have several answers (e.g. via "Ask persona"); controls act on just one.
            self._ensure_columns(
                "messages",
                {"author_id": "INTEGER", "reply_to": "INTEGER", "requester_id": "INTEGER", "answer_id": "INTEGER"},
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_channel ON messages(channel_id, discord_msg_id)")
            self._conn.execute("CREATE TABLE IF NOT EXISTS channel_settings (channel_id INTEGER PRIMARY KEY)")
            self._ensure_columns("channel_settings", SETTING_COLUMNS)
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS custom_personas (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    creator_id INTEGER NOT NULL,
                    name TEXT COLLATE NOCASE NOT NULL,
                    prompt TEXT NOT NULL,
                    created_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(guild_id, name)
                )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS chess_games (
                    channel_id INTEGER PRIMARY KEY,
                    fen TEXT NOT NULL,
                    move_stack TEXT NOT NULL DEFAULT '',
                    started_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS generations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                    channel_id INTEGER NOT NULL,
                    guild_id INTEGER,
                    persona TEXT NOT NULL,
                    model TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    wall_seconds REAL NOT NULL,
                    completion_tokens INTEGER,
                    prompt_tokens INTEGER,
                    tokens_per_second REAL,
                    cold_start INTEGER NOT NULL DEFAULT 0,
                    trimmed INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                )"""
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_generations_ts ON generations(ts)")
            # Uploaded persona pictures, per server. url points at the avatar of webhook_id, a small
            # webhook kept only to host the image (Discord attachment links expire; avatars don't).
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS persona_avatars (
                    guild_id INTEGER NOT NULL,
                    persona TEXT NOT NULL,
                    url TEXT NOT NULL,
                    webhook_id INTEGER,
                    set_by INTEGER,
                    updated_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, persona)
                )"""
            )

    def _ensure_columns(self, table: str, columns: dict[str, str]) -> None:
        existing = {row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns.items():
            if name not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # ── Messages ────────────────────────────────────────────────────

    def save_message(
        self,
        discord_msg_id: int,
        parent_msg_id: int | None,
        channel_id: int,
        role: str,
        content: str,
        author_id: int | None = None,
        reply_to: int | None = None,
        requester_id: int | None = None,
        answer_id: int | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO messages (discord_msg_id, parent_msg_id, channel_id, author_id, role, "
                "content, reply_to, requester_id, answer_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (discord_msg_id, parent_msg_id, channel_id, author_id, role, content, reply_to, requester_id, answer_id),
            )

    def message(self, discord_msg_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT discord_msg_id, parent_msg_id, channel_id, author_id, role, content, reply_to, requester_id, "
            "answer_id FROM messages WHERE discord_msg_id = ?",
            (discord_msg_id,),
        ).fetchone()
        return dict(row) if row else None

    def response_ids(self, request_msg_id: int) -> list[int]:
        """Every bot message (all answers, all chunks) answering one user message, oldest first."""
        rows = self._conn.execute(
            "SELECT discord_msg_id FROM messages WHERE reply_to = ? AND role = 'assistant' ORDER BY discord_msg_id",
            (request_msg_id,),
        ).fetchall()
        return [row["discord_msg_id"] for row in rows]

    def answer_ids(self, discord_msg_id: int) -> list[int]:
        """All chunks of the one answer containing `discord_msg_id`, oldest first."""
        row = self.message(discord_msg_id)
        if row is None or row["role"] != "assistant":
            return []
        if row["answer_id"] is not None:
            condition, params = "answer_id = ?", (row["answer_id"],)
        else:  # rows from before answers were grouped: same request and same requester
            condition, params = "reply_to = ? AND requester_id IS ?", (row["reply_to"], row["requester_id"])
        rows = self._conn.execute(
            f"SELECT discord_msg_id FROM messages WHERE role = 'assistant' AND {condition} ORDER BY discord_msg_id",
            params,
        ).fetchall()
        return [item["discord_msg_id"] for item in rows]

    def delete_messages(self, discord_msg_ids: list[int]) -> None:
        with self._conn:
            self._conn.executemany("DELETE FROM messages WHERE discord_msg_id = ?", [(i,) for i in discord_msg_ids])

    def message_chain(self, start_msg_id: int, channel_id: int, limit: int = 40) -> list[dict]:
        """The reply chain ending at `start_msg_id`, oldest first, within one channel."""
        rows = self._conn.execute(
            """WITH RECURSIVE chain(discord_msg_id, parent_msg_id, author_id, role, content, depth) AS (
                SELECT discord_msg_id, parent_msg_id, author_id, role, content, 0
                FROM messages WHERE discord_msg_id = ? AND channel_id = ?
                UNION ALL
                SELECT m.discord_msg_id, m.parent_msg_id, m.author_id, m.role, m.content, c.depth + 1
                FROM messages m JOIN chain c ON m.discord_msg_id = c.parent_msg_id
                WHERE m.channel_id = ? AND c.depth < ?
            )
            SELECT discord_msg_id, author_id, role, content FROM chain ORDER BY depth DESC""",
            (start_msg_id, channel_id, channel_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def clear_channel(self, channel_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM messages WHERE channel_id = ?", (channel_id,))

    # ── Channel settings ────────────────────────────────────────────

    def channel_settings(self, channel_id: int) -> ChannelSettings:
        row = self._conn.execute(
            f"SELECT {', '.join(SETTING_COLUMNS)} FROM channel_settings WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        if row is None:
            return ChannelSettings()
        defaults = ChannelSettings()
        values = {}
        for item in fields(ChannelSettings):
            value = row[item.name]
            if value is None:
                continue
            values[item.name] = bool(value) if isinstance(getattr(defaults, item.name), bool) else value
        return ChannelSettings(**values)

    def update_channel(self, channel_id: int, **changes: object) -> ChannelSettings:
        unknown = set(changes) - set(SETTING_COLUMNS)
        if unknown:
            raise ValueError(f"Unknown channel settings: {', '.join(sorted(unknown))}")
        if "verbosity" in changes and changes["verbosity"] not in VERBOSITY_LEVELS:
            raise ValueError(f"Unknown verbosity {changes['verbosity']!r}")
        columns = list(changes)
        values = [int(value) if isinstance(value, bool) else value for value in changes.values()]
        with self._conn:
            self._conn.execute(
                f"INSERT INTO channel_settings (channel_id, {', '.join(columns)}) "
                f"VALUES (?, {', '.join('?' for _ in columns)}) "
                f"ON CONFLICT(channel_id) DO UPDATE SET {', '.join(f'{c} = excluded.{c}' for c in columns)}",
                (channel_id, *values),
            )
        return self.channel_settings(channel_id)

    # ── Custom personas ─────────────────────────────────────────────

    def create_custom_persona(self, guild_id: int, creator_id: int, name: str, prompt: str) -> int | None:
        """Returns the new id, or None if the name is taken in this guild."""
        try:
            with self._conn:
                cursor = self._conn.execute(
                    "INSERT INTO custom_personas (guild_id, creator_id, name, prompt) VALUES (?, ?, ?, ?)",
                    (guild_id, creator_id, name.strip(), prompt.strip()),
                )
            return int(cursor.lastrowid)
        except sqlite3.IntegrityError:
            return None

    def custom_persona(self, persona_id: int, guild_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT id, guild_id, creator_id, name, prompt FROM custom_personas WHERE id = ? AND guild_id = ?",
            (persona_id, guild_id),
        ).fetchone()
        return dict(row) if row else None

    def custom_personas(self, guild_id: int) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, guild_id, creator_id, name, prompt FROM custom_personas "
            "WHERE guild_id = ? ORDER BY name COLLATE NOCASE",
            (guild_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def update_custom_persona(self, persona_id: int, guild_id: int, name: str, prompt: str) -> bool:
        """Returns False if the persona is gone or the new name is taken."""
        try:
            with self._conn:
                cursor = self._conn.execute(
                    "UPDATE custom_personas SET name = ?, prompt = ? WHERE id = ? AND guild_id = ?",
                    (name.strip(), prompt.strip(), persona_id, guild_id),
                )
            return cursor.rowcount > 0
        except sqlite3.IntegrityError:
            return False

    def delete_custom_persona(self, persona_id: int, guild_id: int, persona_key: str, fallback_key: str) -> bool:
        """Deletes the persona and moves channels using it to `fallback_key`."""
        with self._conn:
            cursor = self._conn.execute(
                "DELETE FROM custom_personas WHERE id = ? AND guild_id = ?", (persona_id, guild_id)
            )
            if cursor.rowcount:
                self._conn.execute(
                    "UPDATE channel_settings SET persona = ? WHERE persona = ?", (fallback_key, persona_key)
                )
                self._conn.execute(
                    "DELETE FROM persona_avatars WHERE guild_id = ? AND persona = ?", (guild_id, persona_key)
                )
        return cursor.rowcount > 0

    # ── Persona avatars ─────────────────────────────────────────────

    def persona_avatar(self, guild_id: int, persona_key: str) -> dict | None:
        row = self._conn.execute(
            "SELECT persona, url, webhook_id, set_by FROM persona_avatars WHERE guild_id = ? AND persona = ?",
            (guild_id, persona_key),
        ).fetchone()
        return dict(row) if row else None

    def persona_avatars(self, guild_id: int) -> dict[str, str]:
        """persona key → uploaded avatar URL, for one server."""
        rows = self._conn.execute("SELECT persona, url FROM persona_avatars WHERE guild_id = ?", (guild_id,)).fetchall()
        return {row["persona"]: row["url"] for row in rows}

    def set_persona_avatar(self, guild_id: int, persona_key: str, url: str, webhook_id: int | None, set_by: int) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO persona_avatars (guild_id, persona, url, webhook_id, set_by, updated_ts) "
                "VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)",
                (guild_id, persona_key, url, webhook_id, set_by),
            )

    def delete_persona_avatar(self, guild_id: int, persona_key: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM persona_avatars WHERE guild_id = ? AND persona = ?", (guild_id, persona_key))

    # ── Chess ───────────────────────────────────────────────────────

    def chess_moves(self, channel_id: int) -> str | None:
        """The game's moves as space-separated UCI, or None if no game is saved."""
        row = self._conn.execute(
            "SELECT move_stack FROM chess_games WHERE channel_id = ?", (channel_id,)
        ).fetchone()
        return row["move_stack"] if row else None

    def save_chess_game(self, channel_id: int, fen: str, moves: str) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO chess_games (channel_id, fen, move_stack, updated_ts) "
                "VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
                (channel_id, fen, moves),
            )

    def delete_chess_game(self, channel_id: int) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM chess_games WHERE channel_id = ?", (channel_id,))

    # ── Generation log ──────────────────────────────────────────────

    def record_generation(self, generation: Generation) -> None:
        values = {item.name: getattr(generation, item.name) for item in fields(Generation)}
        values = {key: int(value) if isinstance(value, bool) else value for key, value in values.items()}
        with self._conn:
            self._conn.execute(
                f"INSERT INTO generations ({', '.join(values)}) VALUES ({', '.join('?' for _ in values)})",
                tuple(values.values()),
            )

    def generation_stats(self, since: str = "-1 day", guild_id: int | None = None) -> dict:
        """Totals since an SQLite datetime modifier such as '-1 day' or '-7 days'."""
        scope = "ts >= datetime('now', ?)" + (" AND guild_id = ?" if guild_id is not None else "")
        params: tuple = (since, guild_id) if guild_id is not None else (since,)
        totals = self._conn.execute(
            f"""SELECT COUNT(*) AS replies,
                       COALESCE(SUM(ok = 0), 0) AS failures,
                       COALESCE(SUM(cold_start), 0) AS cold_starts,
                       COALESCE(SUM(trimmed), 0) AS trimmed,
                       COALESCE(SUM(completion_tokens), 0) AS tokens,
                       AVG(CASE WHEN ok THEN wall_seconds END) AS avg_seconds,
                       AVG(CASE WHEN ok AND NOT cold_start THEN wall_seconds END) AS avg_warm_seconds,
                       AVG(tokens_per_second) AS avg_tokens_per_second
                FROM generations WHERE {scope}""",
            params,
        ).fetchone()
        personas = self._conn.execute(
            f"SELECT persona, COUNT(*) AS replies FROM generations WHERE {scope} "
            "GROUP BY persona ORDER BY replies DESC, persona LIMIT 5",
            params,
        ).fetchall()
        models = self._conn.execute(
            f"SELECT profile, COUNT(*) AS replies FROM generations WHERE {scope} GROUP BY profile ORDER BY replies DESC",
            params,
        ).fetchall()
        return {
            **dict(totals),
            "personas": [(row["persona"], row["replies"]) for row in personas],
            "profiles": [(row["profile"], row["replies"]) for row in models],
        }
