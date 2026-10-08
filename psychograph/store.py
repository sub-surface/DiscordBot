"""SQLite persistence: reply-chain history, channel settings, custom personas, chess games, debates, predictions."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields
from pathlib import Path

VERBOSITY_LEVELS = ("concise", "balanced", "detailed")
PREDICTION_OUTCOMES = ("right", "wrong", "void")
REVIEW_REPLACES_HOURS = 6   # a new review of the same people in a channel replaces one this recent


@dataclass(frozen=True)
class ChannelSettings:
    persona: str | None = None
    verbosity: str = "balanced"
    chess_commentary: bool = False
    reactions: bool = True           # the persona may react to messages with server emotes
    persona_voice: bool = False      # speak through a webhook as the persona
    sounds: bool = False             # let personas play soundbank clips


SETTING_COLUMNS = {
    "persona": "TEXT",
    "verbosity": "TEXT",
    "chess_commentary": "INTEGER NOT NULL DEFAULT 0",
    "reactions": "INTEGER NOT NULL DEFAULT 1",
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
            # answer (not always that message's author), answer_id the answer's first message, and persona
            # the key of the persona that gave it (replies continue with it). One request can have several
            # answers (e.g. via "Ask persona"); controls act on just one.
            self._ensure_columns(
                "messages",
                {
                    "author_id": "INTEGER",
                    "reply_to": "INTEGER",
                    "requester_id": "INTEGER",
                    "answer_id": "INTEGER",
                    "persona": "TEXT",
                },
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
            # Debate reviews for the leaderboard: one row per review answer. participants are the
            # members who argued, as sorted space-separated ids; winner_id is None for a split decision.
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS debates (
                    answer_id INTEGER PRIMARY KEY,
                    guild_id INTEGER,
                    channel_id INTEGER NOT NULL,
                    winner_id INTEGER,
                    participants TEXT NOT NULL,
                    ts DATETIME DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            # Persona duels: winner is a persona key, or None for a draw.
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS duels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER,
                    channel_id INTEGER NOT NULL,
                    persona_a TEXT NOT NULL,
                    persona_b TEXT NOT NULL,
                    winner TEXT,
                    topic TEXT NOT NULL,
                    ts DATETIME DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            # Small bits of bot state that must survive restarts (e.g. which week's digest went out).
            self._conn.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT)")
            # Members the bot ignores until a moment (unix seconds), per server: /timeout.
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS timeouts (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    until REAL NOT NULL,
                    set_by INTEGER,
                    PRIMARY KEY (guild_id, user_id)
                )"""
            )
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS predictions (
                    message_id INTEGER PRIMARY KEY,
                    guild_id INTEGER,
                    channel_id INTEGER NOT NULL,
                    author_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    logged_by INTEGER,
                    status TEXT NOT NULL DEFAULT 'open',
                    resolved_by INTEGER,
                    ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                    resolved_ts DATETIME
                )"""
            )

        with self._conn:
            self._conn.execute(
                """CREATE TABLE IF NOT EXISTS slop_shares (
                    message_id INTEGER NOT NULL,
                    status_id TEXT NOT NULL,
                    guild_id INTEGER,
                    channel_id INTEGER NOT NULL,
                    sharer_id INTEGER NOT NULL,
                    sharer TEXT NOT NULL,
                    shared_at REAL NOT NULL,
                    reacts INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (message_id, status_id)
                )"""
            )
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS tweet_stats (status_id TEXT PRIMARY KEY, data TEXT NOT NULL, fetched_at REAL NOT NULL)"
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
        persona: str | None = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO messages (discord_msg_id, parent_msg_id, channel_id, author_id, role, "
                "content, reply_to, requester_id, answer_id, persona) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    discord_msg_id, parent_msg_id, channel_id, author_id, role, content,
                    reply_to, requester_id, answer_id, persona,
                ),
            )

    def append_to_message(self, discord_msg_id: int, text: str) -> None:
        """Add context to a stored message (the System 1 notes behind an answer, say)."""
        with self._conn:
            self._conn.execute(
                "UPDATE messages SET content = content || ? WHERE discord_msg_id = ?", (text, discord_msg_id)
            )

    def message(self, discord_msg_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT discord_msg_id, parent_msg_id, channel_id, author_id, role, content, reply_to, requester_id, "
            "answer_id, persona FROM messages WHERE discord_msg_id = ?",
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
            """WITH RECURSIVE chain(discord_msg_id, parent_msg_id, author_id, role, content, persona, depth) AS (
                SELECT discord_msg_id, parent_msg_id, author_id, role, content, persona, 0
                FROM messages WHERE discord_msg_id = ? AND channel_id = ?
                UNION ALL
                SELECT m.discord_msg_id, m.parent_msg_id, m.author_id, m.role, m.content, m.persona, c.depth + 1
                FROM messages m JOIN chain c ON m.discord_msg_id = c.parent_msg_id
                WHERE m.channel_id = ? AND c.depth < ?
            )
            SELECT discord_msg_id, author_id, role, content, persona FROM chain ORDER BY depth DESC""",
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

    # ── Debates ─────────────────────────────────────────────────────

    def record_debate(
        self, answer_id: int, guild_id: int | None, channel_id: int, winner_id: int | None, participants: list[int]
    ) -> None:
        """Record a review's result. A recent review of the same people in the channel is replaced, not added to."""
        people = " ".join(str(user_id) for user_id in sorted(set(participants)))
        with self._conn:
            self._conn.execute(
                "DELETE FROM debates WHERE channel_id = ? AND participants = ? AND ts >= datetime('now', ?)",
                (channel_id, people, f"-{REVIEW_REPLACES_HOURS} hours"),
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO debates (answer_id, guild_id, channel_id, winner_id, participants) "
                "VALUES (?, ?, ?, ?, ?)",
                (answer_id, guild_id, channel_id, winner_id, people),
            )

    def delete_debates(self, answer_ids: list[int]) -> None:
        with self._conn:
            self._conn.executemany("DELETE FROM debates WHERE answer_id = ?", [(i,) for i in answer_ids])

    def debate_table(self, guild_id: int) -> list[dict]:
        """Per member: wins, losses and splits, best record first."""
        table: dict[int, dict] = {}
        for row in self._conn.execute("SELECT winner_id, participants FROM debates WHERE guild_id = ?", (guild_id,)):
            for user_id in map(int, row["participants"].split()):
                record = table.setdefault(user_id, {"user_id": user_id, "wins": 0, "losses": 0, "splits": 0})
                if row["winner_id"] is None:
                    record["splits"] += 1
                else:
                    record["wins" if row["winner_id"] == user_id else "losses"] += 1
        return sorted(table.values(), key=lambda r: (-r["wins"], r["losses"], -r["splits"], r["user_id"]))

    # ── Duels ───────────────────────────────────────────────────────

    def record_duel(
        self, guild_id: int | None, channel_id: int, persona_a: str, persona_b: str, winner: str | None, topic: str
    ) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO duels (guild_id, channel_id, persona_a, persona_b, winner, topic) VALUES (?, ?, ?, ?, ?, ?)",
                (guild_id, channel_id, persona_a, persona_b, winner, topic.strip()),
            )

    def duel_table(self, guild_id: int, since: str = "-100 years") -> list[dict]:
        """Per persona key: wins, losses and draws, best record first."""
        table: dict[str, dict] = {}
        rows = self._conn.execute(
            "SELECT persona_a, persona_b, winner FROM duels WHERE guild_id = ? AND ts >= datetime('now', ?)",
            (guild_id, since),
        )
        for row in rows:
            for persona in (row["persona_a"], row["persona_b"]):
                record = table.setdefault(persona, {"persona": persona, "wins": 0, "losses": 0, "draws": 0})
                if row["winner"] is None:
                    record["draws"] += 1
                else:
                    record["wins" if row["winner"] == persona else "losses"] += 1
        return sorted(table.values(), key=lambda r: (-r["wins"], r["losses"], -r["draws"], r["persona"]))

    # ── Bot state ───────────────────────────────────────────────────

    def state(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._conn:
            self._conn.execute("INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)", (key, value))

    def week_counts(self, guild_id: int) -> dict:
        """What the scoreboards gained in the last seven days, for the Monday digest."""
        since = ("-7 days",)
        return {
            "debates": self._conn.execute(
                "SELECT COUNT(*) FROM debates WHERE guild_id = ? AND ts >= datetime('now', ?)", (guild_id, *since)
            ).fetchone()[0],
            "predictions_logged": self._conn.execute(
                "SELECT COUNT(*) FROM predictions WHERE guild_id = ? AND ts >= datetime('now', ?)", (guild_id, *since)
            ).fetchone()[0],
            "predictions_settled": self._conn.execute(
                "SELECT COUNT(*) FROM predictions WHERE guild_id = ? AND resolved_ts >= datetime('now', ?)",
                (guild_id, *since),
            ).fetchone()[0],
            "duels": self._conn.execute(
                "SELECT COUNT(*) FROM duels WHERE guild_id = ? AND ts >= datetime('now', ?)", (guild_id, *since)
            ).fetchone()[0],
        }

    # ── Timeouts ────────────────────────────────────────────────────

    def set_timeout(self, guild_id: int, user_id: int, until: float, set_by: int) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO timeouts (guild_id, user_id, until, set_by) VALUES (?, ?, ?, ?)",
                (guild_id, user_id, until, set_by),
            )

    def clear_timeout(self, guild_id: int, user_id: int) -> bool:
        with self._conn:
            cursor = self._conn.execute("DELETE FROM timeouts WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        return cursor.rowcount > 0

    def timeout_until(self, guild_id: int | None, user_id: int, now: float) -> float | None:
        """When the member's timeout ends, if it hasn't yet."""
        if guild_id is None:
            return None
        row = self._conn.execute(
            "SELECT until FROM timeouts WHERE guild_id = ? AND user_id = ? AND until > ?", (guild_id, user_id, now)
        ).fetchone()
        return row["until"] if row else None

    # ── Predictions ─────────────────────────────────────────────────

    def add_prediction(
        self, message_id: int, guild_id: int | None, channel_id: int, author_id: int, text: str, logged_by: int
    ) -> bool:
        """False if that message is already logged."""
        with self._conn:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO predictions (message_id, guild_id, channel_id, author_id, text, logged_by) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (message_id, guild_id, channel_id, author_id, text.strip(), logged_by),
            )
        return cursor.rowcount > 0

    def prediction(self, message_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM predictions WHERE message_id = ?", (message_id,)).fetchone()
        return dict(row) if row else None

    def predictions(self, guild_id: int, status: str = "open", limit: int = 25) -> list[dict]:
        """Newest first."""
        rows = self._conn.execute(
            "SELECT * FROM predictions WHERE guild_id = ? AND status = ? ORDER BY message_id DESC LIMIT ?",
            (guild_id, status, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def resolve_prediction(self, message_id: int, outcome: str, resolved_by: int) -> bool:
        """Settle an open prediction as right, wrong or void; False if it's gone or already settled."""
        if outcome not in PREDICTION_OUTCOMES:
            raise ValueError(f"Unknown outcome {outcome!r}")
        with self._conn:
            cursor = self._conn.execute(
                "UPDATE predictions SET status = ?, resolved_by = ?, resolved_ts = CURRENT_TIMESTAMP "
                "WHERE message_id = ? AND status = 'open'",
                (outcome, resolved_by, message_id),
            )
        return cursor.rowcount > 0

    def prediction_table(self, guild_id: int) -> list[dict]:
        """Per member: right, wrong and open predictions, most right first."""
        rows = self._conn.execute(
            """SELECT author_id, SUM(status = 'right') AS "right", SUM(status = 'wrong') AS wrong,
                      SUM(status = 'open') AS open
               FROM predictions WHERE guild_id = ? GROUP BY author_id
               ORDER BY "right" DESC, wrong ASC, open DESC, author_id""",
            (guild_id,),
        ).fetchall()
        return [dict(row) for row in rows]

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

    # ── The slop report: posts shared in a channel, and their cached public stats ──

    def save_share(self, message_id: int, status_id: str, guild_id: int | None, channel_id: int,
                   sharer_id: int, sharer: str, shared_at: float, reacts: int) -> None:
        with self._conn:
            self._conn.execute(
                "INSERT INTO slop_shares VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (message_id, status_id) DO UPDATE SET reacts = excluded.reacts, sharer = excluded.sharer",
                (message_id, status_id, guild_id, channel_id, sharer_id, sharer, shared_at, reacts),
            )

    def shares(self, channel_id: int, since: float = 0.0) -> list[dict]:
        """Oldest first."""
        rows = self._conn.execute(
            "SELECT * FROM slop_shares WHERE channel_id = ? AND shared_at >= ? ORDER BY shared_at", (channel_id, since)
        ).fetchall()
        return [dict(row) for row in rows]

    def tweet_stats(self, status_id: str, max_age: float, now: float) -> str | None:
        row = self._conn.execute(
            "SELECT data FROM tweet_stats WHERE status_id = ? AND fetched_at >= ?", (status_id, now - max_age)
        ).fetchone()
        return row["data"] if row else None

    def save_tweet_stats(self, status_id: str, data: str, now: float) -> None:
        with self._conn:
            self._conn.execute("INSERT OR REPLACE INTO tweet_stats VALUES (?, ?, ?)", (status_id, data, now))
