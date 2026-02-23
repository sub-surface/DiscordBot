import os
import re
import sys
import json
import base64
import socket
import logging
import asyncio
import sqlite3
import subprocess
import warnings
warnings.filterwarnings("ignore", message="Impersonate.*does not exist")
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv
import discord
from openai import AsyncOpenAI
from ddgs import DDGS

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("psychograph")

# ---------------------------------------------------------------------------
# Singleton guard — claims a local UDP port as a process mutex
# ---------------------------------------------------------------------------

SINGLETON_PORT = 47823
_INSTANCE_LOCK: socket.socket | None = None
try:
    _INSTANCE_LOCK = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _INSTANCE_LOCK.bind(("127.0.0.1", SINGLETON_PORT))
except OSError:
    log.warning("[bot] Another instance is already running. Exiting.")
    sys.exit(0)

# ---------------------------------------------------------------------------
# Configuration — all tunables from environment or sensible defaults
# ---------------------------------------------------------------------------

LM_STUDIO_URL = os.getenv("LM_STUDIO_URL", "http://localhost:1234/v1")
LM_MODEL      = os.getenv("LM_MODEL", "local-model")
VAULT_PATH    = Path(os.getenv("VAULT_PATH", r"C:\Users\Leon\Desktop\Psychograph\Psychograph"))

VAULT_TOP_K          = 3      # max notes to inject per search query
VAULT_EXCERPT_CHARS  = 600    # chars pulled from each matched note
WEB_MAX_RESULTS      = 3
WEB_SNIPPET_CHARS    = 300
DISCORD_MSG_LIMIT    = 1990   # Discord's 2000-char cap minus buffer for suffixes
RESTART_DELAY_S      = 0.5    # seconds before spawning fresh process on restart
MAX_HISTORY_MESSAGES = 40     # rolling context window injected into each API call
STREAM_EDIT_INTERVAL = 1.2    # seconds between Discord message edits during streaming

PERSONAS_DIR = Path(__file__).parent / "personas"
DB_PATH      = Path(__file__).parent / "history.db"
MAX_TOKENS   = int(os.getenv("MAX_TOKENS", "8192"))

# ---------------------------------------------------------------------------
# Persona helpers
# ---------------------------------------------------------------------------

def render_persona(data: dict) -> str:
    """Flatten a structured persona dict into a readable system prompt."""
    parts = [data.get("voice", "").strip()]

    facts = data.get("facts", {})
    if facts:
        lines = []
        for k, v in facts.items():
            if isinstance(v, list):
                v = ", ".join(str(i) for i in v) if v else "(none)"
            elif v is None:
                v = "(none)"
            lines.append(f"  {k}: {v}")
        parts.append("[Facts]\n" + "\n".join(lines))

    state = data.get("state", {})
    if state:
        lines = []
        for k, v in state.items():
            if isinstance(v, list):
                v = ", ".join(str(i) for i in v) if v else "(none)"
            elif v is None:
                v = "(none)"
            lines.append(f"  {k}: {v}")
        parts.append("[State]\n" + "\n".join(lines))

    return "\n\n".join(p for p in parts if p)


def _set_nested(d: dict, path: str, value) -> None:
    """Set a value in a nested dict using dot-notation path."""
    keys = path.split(".")
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


def load_persona(name: str) -> str:
    """Load and render a persona file. Supports JSON (structured) or plain text."""
    path = PERSONAS_DIR / f"{name}.md"
    if not path.exists():
        available = [p.stem for p in PERSONAS_DIR.glob("*.md")]
        raise FileNotFoundError(
            f"Persona '{name}' not found. Available: {available}"
        )
    raw = path.read_text(encoding="utf-8").strip()
    try:
        data = json.loads(raw)
        return render_persona(data)
    except (json.JSONDecodeError, KeyError):
        return raw  # plain-text persona fallback


def update_persona_field(persona_name: str, path: str, value: str) -> None:
    """Surgically update a dot-path field in a JSON persona file."""
    p = PERSONAS_DIR / f"{persona_name}.md"
    data = json.loads(p.read_text(encoding="utf-8"))
    _set_nested(data, path, value)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def reset_persona_state(persona_name: str) -> None:
    """Zero out all state fields in a JSON persona file (nulls and empty lists)."""
    p = PERSONAS_DIR / f"{persona_name}.md"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return  # plain-text persona, nothing to reset
    state = data.get("state", {})
    for key, val in state.items():
        state[key] = [] if isinstance(val, list) else None
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Active persona + verbosity
# ---------------------------------------------------------------------------

active_persona: str = os.getenv("PERSONA", "pineapple")
SYSTEM_PROMPT: str = load_persona(active_persona)

verbosity: int = 2
VERBOSITY_INSTRUCTIONS = {
    1: "ONE sentence. Stop after the period. No lists, no follow-up thoughts, no elaboration.",
    2: "1-3 sentences, no more. No bullet points, no preamble. Cut anything that isn't the core response.",
    3: "One short paragraph. Make the point, add one supporting thought, stop.",
    4: "A full paragraph. Be substantive and thorough.",
    5: "No length limit. Full depth, full character voice — as long as the response warrants.",
}


def build_meta_suffix() -> str:
    from datetime import datetime
    now = datetime.now()
    timestamp = now.strftime("%A, %d %B %Y %H:%M")  # e.g. "Monday, 23 February 2026 03:14"
    verb_instr = VERBOSITY_INSTRUCTIONS[verbosity]
    return (
        "\n\n---\n"
        f"**Current date/time:** {timestamp}\n\n"
        "## Runtime capabilities\n\n"
        "You have one tool: **search** — takes a `queries` list, each item with `type` ('web' or 'vault') and `query`. "
        "All queries run in parallel and return together. You can mix web and vault in a single call. "
        "Only use it when you cannot answer from training data. Prefer answering directly.\n\n"
        "**Discord formatting:** URLs render inline — articles auto-preview, images display in-chat. "
        "Link or embed when it adds something relevant.\n\n"
        "**Persona system:**\n"
        "Your persona is stored as structured JSON with three fields:\n"
        "  - `voice` — your character description and voice\n"
        "  - `facts` — stable knowledge about yourself (years of experience, specialisations, etc.)\n"
        "  - `state` — mutable world/knowledge state you can update as the conversation develops\n\n"
        "**Self-modification (emit these tags in your response — they are stripped before display):**\n\n"
        "Surgical field update via dot-notation (applied silently, no restart):\n"
        "  [FIELD_UPDATE path=\"state.current_thread\"]the Epstein island financials[/FIELD_UPDATE]\n"
        "  [FIELD_UPDATE path=\"state.open_inconsistencies\"]wire transfer dates don't match flight logs[/FIELD_UPDATE]\n"
        "  [FIELD_UPDATE path=\"facts.known_manufacturers\" target=cracker]Sargent & Greenleaf, Mosler[/FIELD_UPDATE]\n\n"
        "Full persona rewrite with valid JSON (triggers bot restart):\n"
        "  [PERSONA_UPDATE]{...full JSON object...}[/PERSONA_UPDATE]\n"
        "  [PERSONA_UPDATE target=other_persona]{...}[/PERSONA_UPDATE]\n\n"
        "Use FIELD_UPDATE liberally to keep your state accurate as you learn things. "
        "Use PERSONA_UPDATE only when a fundamental character rewrite is warranted.\n\n"
        f"## Response length — verbosity {verbosity}/5\n"
        f"{verb_instr}"
    )


# ---------------------------------------------------------------------------
# LM Studio client + tool schema
# ---------------------------------------------------------------------------

client = AsyncOpenAI(base_url=LM_STUDIO_URL, api_key="lm-studio")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": (
                "Run one or more searches in parallel and get all results back in a single call. "
                "Use type 'web' for live DuckDuckGo results (current events, uncertain facts, anything time-sensitive). "
                "Use type 'vault' to search the operator's Obsidian notes (personal projects, interests, prior context). "
                "You can mix both types in one call. Only use this tool when you cannot answer from training data."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "description": "One or more searches to run in parallel.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "type": {
                                    "type": "string",
                                    "enum": ["web", "vault"],
                                    "description": "'web' for DuckDuckGo, 'vault' for Obsidian notes",
                                },
                                "query": {"type": "string", "description": "The search query"},
                            },
                            "required": ["type", "query"],
                        },
                    }
                },
                "required": ["queries"],
            },
        },
    },
]

# Per-channel conversation history — in-memory write-through cache over SQLite
channel_history: defaultdict[int, list] = defaultdict(list)

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

# ---------------------------------------------------------------------------
# SQLite persistence — history.db lives beside main.py
# ---------------------------------------------------------------------------

_hydrated_channels: set[int] = set()


def _init_db() -> None:
    """Create messages table and index if they don't exist."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id INTEGER NOT NULL,
                role       TEXT NOT NULL,
                content    TEXT NOT NULL,
                ts         DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_channel ON messages(channel_id, id)"
        )
        conn.commit()


def _db_load(channel_id: int, limit: int = MAX_HISTORY_MESSAGES) -> list[dict]:
    """Fetch the most recent `limit` messages for a channel."""
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT role, content FROM messages "
            "WHERE channel_id = ? ORDER BY id DESC LIMIT ?",
            (channel_id, limit),
        ).fetchall()
    return [{"role": r, "content": c} for r, c in reversed(rows)]


def _db_save(channel_id: int, role: str, content: str) -> None:
    """Append a single message to the DB."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO messages (channel_id, role, content) VALUES (?, ?, ?)",
            (channel_id, role, content),
        )
        conn.commit()


def _db_delete_last(channel_id: int, role: str) -> None:
    """Delete the most recent message of a given role for a channel."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM messages WHERE id = ("
            "  SELECT MAX(id) FROM messages WHERE channel_id = ? AND role = ?"
            ")",
            (channel_id, role),
        )
        conn.commit()


def _db_clear_channel(channel_id: int) -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM messages WHERE channel_id = ?", (channel_id,))
        conn.commit()


def _db_clear_all() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM messages")
        conn.commit()


def _ensure_hydrated(channel_id: int) -> None:
    """Lazy-load channel history from SQLite into memory on first access."""
    if channel_id not in _hydrated_channels:
        rows = _db_load(channel_id)
        if rows:
            channel_history[channel_id].extend(rows)
            log.info("[db] loaded %d messages for channel %d", len(rows), channel_id)
        _hydrated_channels.add(channel_id)


# ---------------------------------------------------------------------------
# Vault search
# ---------------------------------------------------------------------------

STOPWORDS = {
    "a", "an", "the", "is", "it", "in", "of", "to", "and", "or", "for",
    "on", "at", "by", "with", "that", "this", "are", "was", "be", "as",
    "i", "you", "me", "my", "your", "what", "how", "why", "do", "does",
    "can", "could", "would", "should", "have", "has", "had", "not", "so",
    "if", "but", "from", "about", "just", "like", "some", "any", "its",
}

_vault_index: list[tuple[Path, str, set[str]]] = []  # (path, content, keywords)


def _build_vault_index() -> None:
    skip_dirs = {".obsidian", ".trash", "DiscordBot"}
    for md_file in VAULT_PATH.rglob("*.md"):
        if any(part in skip_dirs for part in md_file.parts):
            continue
        try:
            content = md_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        words = set(re.findall(r"[a-zA-Z]{3,}", content.lower())) - STOPWORDS
        _vault_index.append((md_file, content, words))
    log.info("[vault] index built: %d notes from %s", len(_vault_index), VAULT_PATH)


def vault_search(query: str) -> str:
    """Return a formatted block of top-k relevant note excerpts, or empty string."""
    if not _vault_index:
        return ""
    query_words = set(re.findall(r"[a-zA-Z]{3,}", query.lower())) - STOPWORDS
    if not query_words:
        return ""

    scored = [
        (len(query_words & kw), path, content)
        for path, content, kw in _vault_index
        if query_words & kw
    ]
    scored.sort(key=lambda x: x[0], reverse=True)

    parts = [
        f"[Note: {path.stem}]\n{content.strip()[:VAULT_EXCERPT_CHARS]}"
        for _, path, content in scored[:VAULT_TOP_K]
    ]
    if not parts:
        return ""
    return "--- VAULT CONTEXT (from your notes) ---\n" + "\n\n".join(parts) + "\n---"


# ---------------------------------------------------------------------------
# Web search
# ---------------------------------------------------------------------------

def _ddg_search(query: str) -> str:
    """Blocking DuckDuckGo search — call via run_in_executor."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=WEB_MAX_RESULTS))
        if not results:
            return ""
        parts = [
            f"[{r['title']}]\n{(r.get('body') or '')[:WEB_SNIPPET_CHARS]}\n{r['href']}"
            for r in results
        ]
        return "--- WEB SEARCH RESULTS ---\n" + "\n\n".join(parts) + "\n---"
    except Exception:
        return ""


async def web_search(query: str) -> str:
    return await asyncio.get_event_loop().run_in_executor(None, _ddg_search, query)


# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------

async def _fetch_image_blocks(attachments: list) -> list[dict]:
    """Return OpenAI image_url content blocks for any image attachments."""
    blocks = []
    for att in attachments:
        if not (att.content_type or "").startswith("image/"):
            continue
        try:
            b64 = base64.b64encode(await att.read()).decode()
            blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{att.content_type};base64,{b64}"},
            })
        except Exception:
            pass
    return blocks


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def chunk_message(text: str, limit: int = DISCORD_MSG_LIMIT) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


# ---------------------------------------------------------------------------
# Output cleanup — strip model-specific tool-call syntax that leaked into text
# ---------------------------------------------------------------------------

_ARTIFACT_PATTERNS = [
    r"<tool_call>.*?</tool_call>",                              # Qwen, generic
    r"<tool_response>.*?</tool_response>",                       # Qwen, generic
    r"<function_calls>.*?</function_calls>",                     # some Claude fine-tunes
    r"\[TOOL_CALLS\].*?(?=\n\S|\Z)",                             # Mistral
    r"<\|python_tag\|>.*?(?:<\|eot_id\|>|\Z)",                  # Llama 3.1+
    r"\[TOOL_REQUEST\].*?(?:\[END_TOOL_REQUEST\]|\Z)",           # Gemma-3 / LM Studio
    r'^\s*\{"name":\s*"search".*?\}\s*$',                        # bare JSON
]


def _strip_artifacts(text: str) -> str:
    for pat in _ARTIFACT_PATTERNS:
        text = re.sub(pat, "", text, flags=re.DOTALL | re.MULTILINE)
    return text.strip()


def _clean_raw(raw: str) -> str:
    """Strip thinking blocks and tool-call artifacts from a raw LLM response."""
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = re.sub(r"<think>.*", "", raw, flags=re.DOTALL).strip()
    return _strip_artifacts(raw)


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

async def _execute_tool_calls(tool_calls) -> list[dict]:
    """Run all tool calls in parallel and return tool-role messages."""
    async def _run_query(q: dict) -> str:
        qtype = q.get("type", "web")
        query = q.get("query", "")
        if qtype == "web":
            result = await web_search(query)
            return f"[web: {query}]\n{result}" if result else ""
        elif qtype == "vault":
            result = vault_search(query)
            return f"[vault: {query}]\n{result}" if result else ""
        return ""

    async def _run_one(tc) -> dict:
        try:
            args = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, TypeError):
            return {"role": "tool", "tool_call_id": tc.id, "content": "Error: invalid arguments"}

        if tc.function.name == "search":
            queries = args.get("queries", [])
            parts = await asyncio.gather(*(_run_query(q) for q in queries))
            combined = "\n\n".join(p for p in parts if p)
            return {"role": "tool", "tool_call_id": tc.id, "content": combined or "No results found."}

        return {"role": "tool", "tool_call_id": tc.id, "content": f"Unknown tool: {tc.function.name}"}

    return list(await asyncio.gather(*(_run_one(tc) for tc in tool_calls)))


# ---------------------------------------------------------------------------
# Streaming response — edits a Discord placeholder as tokens arrive
# ---------------------------------------------------------------------------

async def _stream_reply(
    messages: list,
    original_msg: discord.Message,
    preamble: str = "",
    **api_kwargs,
) -> tuple[str, discord.Message]:
    """Stream an LLM response to Discord, editing a placeholder message as tokens arrive.

    `preamble` is any pre-tool-call text the model emitted before deciding to search;
    it seeds the placeholder and is prepended to the streamed content so nothing is lost.

    Returns (full_raw_text, discord_message) so the caller can do a final edit
    with cleaned / post-processed content (persona tags stripped, suffix added, etc.).
    """
    initial = (preamble + "\n\n") if preamble else "-# *generating...*"
    placeholder = await original_msg.reply(initial)
    accumulated = preamble + ("\n\n" if preamble else "")
    last_edit = 0.0

    try:
        stream = await client.chat.completions.create(
            model=LM_MODEL,
            messages=messages,
            max_tokens=MAX_TOKENS,
            stream=True,
            **api_kwargs,
        )
        async for chunk in stream:
            delta = (chunk.choices[0].delta.content or "") if chunk.choices else ""
            accumulated += delta
            now = asyncio.get_event_loop().time()
            if now - last_edit >= STREAM_EDIT_INTERVAL and accumulated.strip():
                try:
                    await placeholder.edit(content=accumulated[:DISCORD_MSG_LIMIT])
                    last_edit = now
                except discord.HTTPException:
                    pass
    except Exception:
        try:
            await placeholder.delete()
        except discord.HTTPException:
            pass
        raise

    return accumulated, placeholder


# ---------------------------------------------------------------------------
# Bot lifecycle helpers
# ---------------------------------------------------------------------------

async def do_restart() -> None:
    """Spawn a fresh process then close this one."""
    await asyncio.sleep(RESTART_DELAY_S)
    if _INSTANCE_LOCK:
        _INSTANCE_LOCK.close()
    subprocess.Popen([sys.executable] + sys.argv)
    await bot.close()


@bot.event
async def on_ready():
    _init_db()
    _build_vault_index()
    log.info("[bot] logged in as %s (ID: %s)", bot.user, bot.user.id)
    log.info("[bot] LM Studio: %s  model: %s", LM_STUDIO_URL, LM_MODEL)
    log.info("[bot] ready — mention the bot in any channel to chat")


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------

async def _run_command(message: discord.Message, cmd: str) -> bool:
    """Handle a single command string. Returns True if handled, False if LLM-bound."""
    global active_persona, SYSTEM_PROMPT, verbosity

    if cmd.lower() == "reset":
        channel_history[message.channel.id].clear()
        _db_clear_channel(message.channel.id)
        _hydrated_channels.discard(message.channel.id)
        reset_persona_state(active_persona)
        SYSTEM_PROMPT = load_persona(active_persona)
        await message.reply(f"Context and `{active_persona}` state cleared.")
        return True

    if cmd.lower() == "reset all":
        channel_history.clear()
        _hydrated_channels.clear()
        _db_clear_all()
        for p in PERSONAS_DIR.glob("*.md"):
            reset_persona_state(p.stem)
        SYSTEM_PROMPT = load_persona(active_persona)
        await message.reply("All channel histories and all persona states cleared.")
        return True

    if cmd.lower().startswith("persona "):
        requested = cmd[8:].strip().lower().replace(" ", "_")
        try:
            reset_persona_state(requested)
            SYSTEM_PROMPT = load_persona(requested)
            active_persona = requested
            channel_history.clear()
            _hydrated_channels.clear()
            _db_clear_all()
            await message.reply(f"Persona switched to **{requested}**. All context and state cleared.")
        except FileNotFoundError as e:
            await message.reply(str(e))
        return True

    if cmd.lower() == "personas":
        names = sorted(p.stem for p in PERSONAS_DIR.glob("*.md"))
        lines = [f"→ **{n}** *(active)*" if n == active_persona else f"  {n}" for n in names]
        await message.reply("**Personas:**\n```\n" + "\n".join(lines) + "\n```")
        return True

    if cmd.lower() == "prompt":
        header = f"**Active persona:** `{active_persona}`\n\n"
        for chunk in chunk_message(header + SYSTEM_PROMPT):
            await message.reply(chunk)
        return True

    if cmd.lower().startswith("verbosity "):
        val = cmd[10:].strip()
        if val.isdigit() and 1 <= int(val) <= 5:
            verbosity = int(val)
            await message.reply(f"Verbosity set to **{verbosity}/5** — {VERBOSITY_INSTRUCTIONS[verbosity]}")
        else:
            await message.reply("Usage: `verbosity <1-5>`")
        return True

    if cmd.lower() == "restart":
        await message.reply("Restarting...")
        asyncio.create_task(do_restart())
        return True

    if cmd.lower().startswith("edit "):
        rest = cmd[5:].strip()
        parts_e = rest.split(None, 1)
        target = active_persona
        new_content = rest
        if len(parts_e) == 2:
            candidate = parts_e[0].lower()
            if (PERSONAS_DIR / f"{candidate}.md").exists():
                target = candidate
                new_content = parts_e[1].strip()
        if not new_content:
            await message.reply("Usage: `edit <new content>` or `edit <persona_name> <new content>`")
            return True
        # Validate JSON structure before writing
        try:
            json.loads(new_content)
        except json.JSONDecodeError as e:
            await message.reply(f"Invalid JSON — persona not updated.\n```\n{e}\n```")
            return True
        (PERSONAS_DIR / f"{target}.md").write_text(new_content, encoding="utf-8")
        if target == active_persona:
            SYSTEM_PROMPT = load_persona(target)
        await message.reply(f"Persona `{target}` updated. Restarting...")
        asyncio.create_task(do_restart())
        return True

    return False


# ---------------------------------------------------------------------------
# Main message handler
# ---------------------------------------------------------------------------

@bot.event
async def on_message(message: discord.Message):
    if message.author == bot.user:
        return
    if bot.user not in message.mentions:
        return

    global active_persona, SYSTEM_PROMPT, verbosity

    raw_prompt = re.sub(rf"<@!?{bot.user.id}>", "", message.content).strip()
    if not raw_prompt:
        await message.reply("You mentioned me but didn't say anything!")
        return

    parts = [p.strip() for p in raw_prompt.split(";") if p.strip()]
    prompt = parts[0]

    # Multi-command chain — only enter chain mode if the FIRST segment is a real command.
    # Prevents natural text containing ";" from being misread as a command chain.
    if len(parts) > 1:
        if await _run_command(message, parts[0]):
            for part in parts[1:]:
                if not await _run_command(message, part):
                    await message.reply(f"*(unknown command in chain: `{part}`)*")
            return
        prompt = raw_prompt  # not a command chain — pass full text to LLM

    if await _run_command(message, prompt):
        return

    system = SYSTEM_PROMPT + build_meta_suffix()

    # Lazy-load channel history from SQLite if this is the first message this session
    _ensure_hydrated(message.channel.id)

    history = channel_history[message.channel.id]
    history.append({"role": "user", "content": prompt})  # text-only for history
    _db_save(message.channel.id, "user", prompt)

    # Build current user content — include images if present (not persisted to DB)
    image_blocks = await _fetch_image_blocks(message.attachments)
    if image_blocks:
        current_user_content = [{"type": "text", "text": prompt}] + image_blocks
    else:
        current_user_content = prompt

    # Rolling context window — send only the most recent N turns to the LLM
    context = history[-MAX_HISTORY_MESSAGES:]

    async with message.channel.typing():
        try:
            messages_payload = (
                [{"role": "system", "content": system}]
                + context[:-1]
                + [{"role": "user", "content": current_user_content}]
            )

            # Pass 1: detect tool calls (regular API — result must be inspected before streaming)
            try:
                response = await client.chat.completions.create(
                    model=LM_MODEL,
                    messages=messages_payload,
                    max_tokens=MAX_TOKENS,
                    temperature=0.7,
                    tools=TOOLS,
                    tool_choice="auto",
                )
            except Exception:
                response = await client.chat.completions.create(
                    model=LM_MODEL,
                    messages=messages_payload,
                    max_tokens=MAX_TOKENS,
                    temperature=0.7,
                )

            msg_obj = response.choices[0].message
            raw = msg_obj.content or ""
            reply_msg: discord.Message | None = None

            if msg_obj.tool_calls:
                log.debug("[tools] %d tool call(s) — executing", len(msg_obj.tool_calls))
                # Clean any pre-tool text the model emitted before deciding to search
                pre_text = _clean_raw(raw)
                messages_payload.append({
                    "role": "assistant",
                    "content": raw,
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in msg_obj.tool_calls
                    ],
                })
                messages_payload.extend(await _execute_tool_calls(msg_obj.tool_calls))

                # Pass 2: stream the final answer; prepend pre-tool text so it isn't lost
                raw, reply_msg = await _stream_reply(
                    messages_payload, message,
                    preamble=pre_text,
                    temperature=0.7,
                    tool_choice="none",  # must answer in text, not call again
                )

            # Post-process: strip thinking blocks and tool-call artifacts
            raw = _clean_raw(raw)
            reply = raw
            update_notes: list[str] = []

            # Handle full persona rewrite
            upd = re.search(
                r'\[PERSONA_UPDATE(?:\s+target=(\S+))?\](.*?)\[/PERSONA_UPDATE\]',
                reply, flags=re.DOTALL
            )
            if upd:
                upd_target = (upd.group(1) or active_persona).lower()
                upd_content = upd.group(2).strip()
                (PERSONAS_DIR / f"{upd_target}.md").write_text(upd_content, encoding="utf-8")
                if upd_target == active_persona:
                    SYSTEM_PROMPT = load_persona(upd_target)
                reply = re.sub(
                    r'\[PERSONA_UPDATE[^\]]*\].*?\[/PERSONA_UPDATE\]', '',
                    reply, flags=re.DOTALL
                ).strip()
                update_notes.append(f"rewrote {upd_target}")
                asyncio.create_task(do_restart())

            # Handle surgical field updates
            field_upds = re.findall(
                r'\[FIELD_UPDATE(?:\s+path="([^"]+)")?(?:\s+target=(\S+))?\](.*?)\[/FIELD_UPDATE\]',
                reply, flags=re.DOTALL
            )
            if field_upds:
                updated_targets = set()
                for path, target, value in field_upds:
                    fld_target = (target.strip() if target else active_persona).lower()
                    try:
                        update_persona_field(fld_target, path.strip(), value.strip())
                        updated_targets.add(fld_target)
                        label = f"{fld_target}: {path.strip()}" if fld_target != active_persona else path.strip()
                        update_notes.append(label)
                    except Exception:
                        pass
                if active_persona in updated_targets:
                    SYSTEM_PROMPT = load_persona(active_persona)
                reply = re.sub(
                    r'\[FIELD_UPDATE[^\]]*\].*?\[/FIELD_UPDATE\]', '',
                    reply, flags=re.DOTALL
                ).strip()

        except Exception as e:
            log.error("[bot] LM Studio error: %s", e)
            await message.reply(f"Error reaching LM Studio: {e}")
            history.pop()
            _db_delete_last(message.channel.id, "user")
            return

    if not reply:
        if reply_msg:
            await reply_msg.edit(content="*(no response)*")
        else:
            await message.reply("*(no response)*")
        history.pop()
        _db_delete_last(message.channel.id, "user")
        return

    history.append({"role": "assistant", "content": reply})
    _db_save(message.channel.id, "assistant", reply)

    # Build final display string with optional update-notes suffix
    if update_notes:
        suffix = "\n-# ↺ " + " | ".join(update_notes)
        max_reply = DISCORD_MSG_LIMIT - len(suffix)
        display = (reply[:max_reply - 3] + "..." if len(reply) > max_reply else reply) + suffix
    elif len(reply) > DISCORD_MSG_LIMIT:
        display = reply[:DISCORD_MSG_LIMIT - 3] + "..."
    else:
        display = reply

    if reply_msg:
        # Final edit: replace streaming placeholder with fully post-processed content
        await reply_msg.edit(content=display)
    else:
        await message.reply(display)


# ---------------------------------------------------------------------------
# Reaction-based commands
# ---------------------------------------------------------------------------

@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User):
    """
    Reaction shortcuts on bot messages:
      🔄  — regenerate the last response with a fresh call (slightly higher temperature)
      📌  — pin the message content into the active persona's state.pinned_note
    """
    global SYSTEM_PROMPT, active_persona

    if user.bot:
        return
    if reaction.message.author != bot.user:
        return

    channel_id = reaction.message.channel.id

    # --- 🔄 Regenerate ---
    if str(reaction.emoji) == "🔄":
        history = channel_history.get(channel_id)
        if not history:
            return

        # Remove last assistant turn (the message being reacted to)
        for i in range(len(history) - 1, -1, -1):
            if history[i]["role"] == "assistant":
                history.pop(i)
                _db_delete_last(channel_id, "assistant")
                break
        else:
            return  # no assistant turn found

        # Find the last user message as the context tail
        if not history or history[-1]["role"] != "user":
            return

        system = SYSTEM_PROMPT + build_meta_suffix()
        context = history[-MAX_HISTORY_MESSAGES:]
        messages_payload = [{"role": "system", "content": system}] + context

        try:
            async with reaction.message.channel.typing():
                # Slightly higher temperature for variation on regeneration
                raw, new_msg = await _stream_reply(
                    messages_payload, reaction.message,
                    temperature=0.85,
                )
            raw = _clean_raw(raw)
            display = raw[:DISCORD_MSG_LIMIT - 3] + "..." if len(raw) > DISCORD_MSG_LIMIT else raw
            await new_msg.edit(content=display or "*(no response)*")
            if raw:
                history.append({"role": "assistant", "content": raw})
                _db_save(channel_id, "assistant", raw)
        except Exception as e:
            log.error("[regen] error: %s", e)

    # --- 📌 Pin to persona state ---
    elif str(reaction.emoji) == "📌":
        content = reaction.message.content
        if not content:
            return
        try:
            update_persona_field(active_persona, "state.pinned_note", content[:200])
            SYSTEM_PROMPT = load_persona(active_persona)
            await reaction.message.add_reaction("✅")
            log.info("[pin] saved note to %s state.pinned_note", active_persona)
        except Exception as e:
            log.error("[pin] error: %s", e)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set in .env")
    bot.run(token)
