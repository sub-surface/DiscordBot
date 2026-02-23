import os
import re
import sys
import json
import base64
import asyncio
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

LM_STUDIO_URL = "http://localhost:1234/v1"
VAULT_PATH = Path(r"C:\Users\Leon\Desktop\Psychograph\Psychograph")
VAULT_TOP_K = 3          # max notes to inject per query
VAULT_EXCERPT_CHARS = 600  # chars to pull from each matched note

PERSONAS_DIR = Path(__file__).parent / "personas"
MAX_TOKENS = int(os.getenv("MAX_TOKENS", "8192"))


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
        return raw  # plain text fallback


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


# Active persona — change PERSONA in .env or switch at runtime with "@bot persona <name>"
active_persona: str = os.getenv("PERSONA", "pineapple")
SYSTEM_PROMPT: str = load_persona(active_persona)

# Verbosity level 1-5. Controls response length instruction injected into system prompt.
verbosity: int = 2
VERBOSITY_INSTRUCTIONS = {
    1: "One sentence only. Extremely terse.",
    2: "1-3 sentences. Match the length of a typical Discord message.",
    3: "A short paragraph. Some elaboration is fine.",
    4: "A full paragraph. Be thorough.",
    5: "No length restriction. Full character voice.",
}


def build_meta_suffix() -> str:
    verb_instr = VERBOSITY_INSTRUCTIONS[verbosity]
    return (
        "\n\n---\n"
        "## Runtime capabilities\n\n"
        "You have two tools: **web_search** and **vault_search**. "
        "Use them only when you genuinely cannot answer from your training data — live scores, breaking news, very recent events, or topics that are clearly specific to the operator's personal notes. "
        "Do not call tools for questions you can answer confidently from knowledge. Prefer answering directly.\n\n"
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
        f"**Verbosity: {verbosity}/5** — {verb_instr}"
    )

client = AsyncOpenAI(base_url=LM_STUDIO_URL, api_key="lm-studio")

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current information. Use when the question concerns recent events, facts you're uncertain about, or anything that benefits from live data.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "vault_search",
            "description": "Search the operator's Obsidian notes for relevant context. Use when the question might relate to the operator's personal notes, projects, or interests.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"}
                },
                "required": ["query"],
            },
        },
    },
]

# Per-channel conversation history: {channel_id: [{"role": ..., "content": ...}]}
channel_history = defaultdict(list)

intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)

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
    print(f"Vault index built: {len(_vault_index)} notes from {VAULT_PATH}")


def vault_search(query: str) -> str:
    """Return a formatted block of top-k relevant note excerpts, or empty string."""
    if not _vault_index:
        return ""
    query_words = set(re.findall(r"[a-zA-Z]{3,}", query.lower())) - STOPWORDS
    if not query_words:
        return ""

    scored = []
    for path, content, keywords in _vault_index:
        score = len(query_words & keywords)
        if score > 0:
            scored.append((score, path, content))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = scored[:VAULT_TOP_K]

    if not top:
        return ""

    parts = []
    for score, path, content in top:
        excerpt = content.strip()[:VAULT_EXCERPT_CHARS]
        parts.append(f"[Note: {path.stem}]\n{excerpt}")

    return "--- VAULT CONTEXT (from your notes) ---\n" + "\n\n".join(parts) + "\n---"


# ---------------------------------------------------------------------------
# Web search
# ---------------------------------------------------------------------------

WEB_MAX_RESULTS = 3
WEB_SNIPPET_CHARS = 300


def _ddg_search(query: str) -> str:
    """Blocking DuckDuckGo search — run in executor."""
    try:
        with DDGS() as ddgs:
            results = list(ddgs.text(query, max_results=WEB_MAX_RESULTS))
        if not results:
            return ""
        parts = []
        for r in results:
            snippet = (r.get("body") or "")[:WEB_SNIPPET_CHARS]
            parts.append(f"[{r['title']}]\n{snippet}\n{r['href']}")
        return "--- WEB SEARCH RESULTS ---\n" + "\n\n".join(parts) + "\n---"
    except Exception:
        return ""


async def web_search(query: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _ddg_search, query)


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
            data = await att.read()
            b64 = base64.b64encode(data).decode()
            blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{att.content_type};base64,{b64}"},
            })
        except Exception:
            pass
    return blocks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def chunk_message(text: str, limit: int = 1990) -> list[str]:
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        chunks.append(text[:limit])
        text = text[limit:]
    return chunks


# ---------------------------------------------------------------------------
# Output cleanup
# ---------------------------------------------------------------------------

# Patterns small models use when they "write" a tool call in text instead of
# using the structured API field — strip these before sending to Discord.
_ARTIFACT_PATTERNS = [
    r"<tool_call>.*?</tool_call>",              # Qwen, generic
    r"<tool_response>.*?</tool_response>",       # Qwen, generic
    r"<function_calls>.*?</function_calls>",     # some Claude-fine-tuned models
    r"\[TOOL_CALLS\].*?(?=\n\S|\Z)",             # Mistral
    r"<\|python_tag\|>.*?(?:<\|eot_id\|>|\Z)",  # Llama 3.1+
    r'^\s*\{"name":\s*"(?:web_search|vault_search)".*?\}\s*$',  # bare JSON
]


def _strip_artifacts(text: str) -> str:
    for pat in _ARTIFACT_PATTERNS:
        text = re.sub(pat, "", text, flags=re.DOTALL | re.MULTILINE)
    return text.strip()


# ---------------------------------------------------------------------------
# Tool execution
# ---------------------------------------------------------------------------

async def _execute_tool_calls(tool_calls) -> list[dict]:
    """Run all tool calls in parallel and return tool-role messages."""
    async def _run_one(tc) -> dict:
        try:
            args = json.loads(tc.function.arguments)
        except (json.JSONDecodeError, TypeError):
            return {"role": "tool", "tool_call_id": tc.id, "content": "Error: invalid arguments"}

        fn = tc.function.name
        if fn == "web_search":
            result = await web_search(args.get("query", ""))
        elif fn == "vault_search":
            result = vault_search(args.get("query", ""))
        else:
            result = f"Unknown tool: {fn}"

        return {"role": "tool", "tool_call_id": tc.id, "content": result or "No results found."}

    return list(await asyncio.gather(*(_run_one(tc) for tc in tool_calls)))


# ---------------------------------------------------------------------------
# Bot events
# ---------------------------------------------------------------------------

async def do_restart():
    """Spawn a fresh process then close this one."""
    await asyncio.sleep(0.5)          # let any pending Discord messages deliver
    subprocess.Popen([sys.executable] + sys.argv)
    await bot.close()                 # discord.py winds down, bot.run() returns


@bot.event
async def on_ready():
    _build_vault_index()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"LM Studio endpoint: {LM_STUDIO_URL}")
    print("Ready. Mention the bot in a channel to chat.")


async def _run_command(message: discord.Message, cmd: str) -> bool:
    """Handle a single command string. Returns True if handled, False if it should go to the LLM."""
    global active_persona, SYSTEM_PROMPT, verbosity

    if cmd.lower() == "reset":
        channel_history[message.channel.id].clear()
        reset_persona_state(active_persona)
        SYSTEM_PROMPT = load_persona(active_persona)
        await message.reply(f"Context and `{active_persona}` state cleared.")
        return True

    if cmd.lower() == "reset all":
        channel_history.clear()
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
        (PERSONAS_DIR / f"{target}.md").write_text(new_content, encoding="utf-8")
        if target == active_persona:
            SYSTEM_PROMPT = load_persona(target)
        await message.reply(f"Persona `{target}` updated. Restarting...")
        asyncio.create_task(do_restart())
        return True

    return False


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
        # Not a command chain — pass the full original text to the LLM
        prompt = raw_prompt

    # Single segment (or non-command text) — try as command, fall through to LLM
    if await _run_command(message, prompt):
        return

    system = SYSTEM_PROMPT + build_meta_suffix()

    history = channel_history[message.channel.id]
    history.append({"role": "user", "content": prompt})  # text-only for history

    # Build current user content — include images if present
    image_blocks = await _fetch_image_blocks(message.attachments)
    if image_blocks:
        current_user_content = [{"type": "text", "text": prompt}] + image_blocks
    else:
        current_user_content = prompt

    async with message.channel.typing():
        try:
            messages = (
                [{"role": "system", "content": system}]
                + history[:-1]
                + [{"role": "user", "content": current_user_content}]
            )

            # Try with tool calling; fall back if the model/server doesn't support it
            try:
                response = await client.chat.completions.create(
                    model="local-model",
                    messages=messages,
                    max_tokens=MAX_TOKENS,
                    temperature=0.7,
                    tools=TOOLS,
                    tool_choice="auto",
                )
            except Exception:
                response = await client.chat.completions.create(
                    model="local-model",
                    messages=messages,
                    max_tokens=MAX_TOKENS,
                    temperature=0.7,
                )
            msg = response.choices[0].message
            raw = msg.content or ""

            if msg.tool_calls:
                # Append the assistant turn (with its tool_calls) then the results
                messages.append({
                    "role": "assistant",
                    "content": raw,
                    "tool_calls": [
                        {"id": tc.id, "type": "function",
                         "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                        for tc in msg.tool_calls
                    ],
                })
                messages.extend(await _execute_tool_calls(msg.tool_calls))

                response2 = await client.chat.completions.create(
                    model="local-model",
                    messages=messages,
                    max_tokens=MAX_TOKENS,
                    temperature=0.7,
                    tool_choice="none",  # follow-up must answer, not call again
                )
                raw = response2.choices[0].message.content or ""

            # Strip reasoning blocks — handle both closed and unclosed <think> tags
            raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
            raw = re.sub(r"<think>.*", "", raw, flags=re.DOTALL).strip()
            # Strip model-specific tool-call syntax that leaked into text content
            raw = _strip_artifacts(raw)

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
            await message.reply(f"Error reaching LM Studio: {e}")
            history.pop()
            return

    if not reply:
        await message.reply("*(no response)*")
        history.pop()
        return

    history.append({"role": "assistant", "content": reply})

    if update_notes:
        suffix = "\n-# ↺ " + " | ".join(update_notes)
        max_reply = 1990 - len(suffix)
        reply = (reply[:max_reply - 3] + "..." if len(reply) > max_reply else reply) + suffix
    elif len(reply) > 1990:
        reply = reply[:1987] + "..."

    await message.reply(reply)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set in .env")
    bot.run(token)
