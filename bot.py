import asyncio
import io
import logging
import random
import os
import re
import socket
import subprocess
import sys
import warnings
from datetime import datetime
from pathlib import Path

import discord
import yaml
from dotenv import load_dotenv

warnings.filterwarnings("ignore", message="Impersonate.*does not exist")
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")

# Singleton guard — UDP port mutex
SINGLETON_PORT = 47823
_INSTANCE_LOCK: socket.socket | None = None
try:
    _INSTANCE_LOCK = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    _INSTANCE_LOCK.bind(("127.0.0.1", SINGLETON_PORT))
except OSError:
    log.warning("Another instance is already running. Exiting.")
    sys.exit(0)

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def save_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(cfg, f, default_flow_style=False, allow_unicode=True)


config = load_config()

current_provider: str = config.get("default_provider", "local")
current_model: str = config.get("default_model", "local-model")
chess_level: int = 3  # chess-classic difficulty; 3 ≈ 1500 Elo (see CHESS_LEVEL_MAP)

DISCORD_MSG_LIMIT = 1990
STREAM_EDIT_INTERVAL = 1.2

# Maps user-facing level (1-8) → (search depth, Elo label)
CHESS_LEVEL_MAP = {
    1: (1,  "~900"),
    2: (3,  "~1200"),
    3: (5,  "~1500"),
    4: (7,  "~1700"),
    5: (9,  "~2000"),
    6: (11, "~2200"),
    7: (13, "~2400"),
    8: (15, "~2600+"),
}

VERBOSITY_INSTRUCTIONS = {
    1: "ONE sentence. Stop after the period. No lists, no follow-up thoughts, no elaboration.",
    2: "1-3 sentences, no more. No bullet points, no preamble. Cut anything that isn't the core response.",
    3: "One short paragraph. Make the point, add one supporting thought, stop.",
    4: "A full paragraph. Be substantive and thorough.",
    5: "No length limit. Full depth, full character voice — as long as the response warrants.",
}

_THINKING_LINES = [
    "-# *· thinking ·*",
    "-# *· gathering thoughts ·*",
    "-# *· consulting the void ·*",
    "-# *· ✦ reaching into the ether ✦ ·*",
    "-# *· ˚ · brewing a response · ˚ ·*",
    "-# *· rummaging through neurons ·*",
    "-# *· ⋆ something stirs ⋆ ·*",
    "-# *· words are forming ·*",
    "-# *· ✧ contemplating ✧ ·*",
    "-# *· turning this over ·*",
    "-# *· ˚ · hold on · ˚ ·*",
    "-# *· ⊹ thinking very hard ⊹ ·*",
    "-# *· sifting through the noise ·*",
    "-# *· ★ · working on it · ★ ·*",
    "-# *· give it a moment ·*",
    "-# *· ✦ · the gears turn · ✦ ·*",
    "-# *· assembling something ·*",
    "-# *· ˖ ° . searching . ° ˖ ·*",
    "-# *· in the quiet before words ·*",
    "-# *· ⋆ ˚ · nearly there · ˚ ⋆ ·*",
]

VERBOSITY_LABELS = {
    1: "·˚ whisper mode · one sentence, then silence ˚·",
    2: "·˚ concise · a breath, not a speech ˚·",
    3: "·˚ balanced · a thought, fully formed ˚·",
    4: "·˚ expansive · room to stretch out ˚·",
    5: "·˚ unbound · full depth, full voice, no ceiling ˚·",
}

import db
import llm
import chess_api
import chess_engine
from board import fen_to_board, fen_to_image
from personas import list_personas, load_persona, load_persona_style
from styles import get_style, make_embed, EMBED_DESC_LIMIT

def ch_persona(channel_id: int) -> str:
    """Active persona for a channel; falls back to the config default."""
    return db.get_channel_persona(channel_id) or config.get("persona", "mochi")


def ch_verbosity(channel_id: int) -> int:
    """Verbosity level for a channel; defaults to 2."""
    return db.get_channel_verbosity(channel_id)


MAX_CHESS_RETRIES = 3

# ── Chess result flavour text ─────────────────────────────────────────
_WIN_MSGS = [
    "✦ · checkmate · you win · ✦",
    "~*~ stockfish has fallen ~*~",
    "· ★ · you beat the engine · ★ ·",
    "*˚· checkmate! well played ·˚*",
    "✧ the machine bows to you ✧",
    "·:· checkmate · a stunning finish ·:·",
    "~ ✦ ~ you outplayed stockfish ~ ✦ ~",
]

_LOSS_MSGS = [
    "✦ checkmate · the engine prevails ✦",
    "~*~ stockfish wins this one ~*~",
    "· ★ · no mercy from the machine · ★ ·",
    "*˚· checkmate · better luck next time ·˚*",
    "✧ outplayed · the engine is ruthless ✧",
    "·:· checkmate · the machine never sleeps ·:·",
    "~ ✦ ~ stockfish sends its regards ~ ✦ ~",
]

_DRAW_MSGS = [
    "·˚ draw · a perfectly balanced game ˚·",
    "~*~ neither side could break through ~*~",
    "✦ · draw · honour preserved on both sides · ✦",
    "*˚· stalemate · a hard-fought result ·˚*",
    "✧ draw · well contested ✧",
    "·:· a draw · the position holds ·:·",
    "~ ✦ ~ no winner today · a fair split ~ ✦ ~",
]

_RESIGN_MSGS = [
    "·˚ you resigned · the engine accepts ˚·",
    "~*~ white tips the king ~*~",
    "✦ white resigns · black wins ✦",
    "*˚· a wise decision · resigned ·˚*",
    "✧ resigned · fight again another day ✧",
    "·:· you resigned · until next time ·:·",
    "~ ✦ ~ white puts down the pieces ~ ✦ ~",
]


def _chess_result_text(status: str) -> str:
    """Pick a random flavour message matching the game outcome."""
    s = status.lower()
    if "white wins" in s:
        return random.choice(_WIN_MSGS)
    if "black wins" in s:
        return random.choice(_LOSS_MSGS)
    return random.choice(_DRAW_MSGS)


# ── UI Views ──────────────────────────────────────────────────────────────────

class ResponseView(discord.ui.View):
    """Persistent action row attached to every LLM response."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="↺ regenerate", style=discord.ButtonStyle.secondary, custom_id="psychograph:regen")
    async def regen(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        bot_row = db.get_message(interaction.message.id)
        if not bot_row:
            return
        user_row = db.get_message(bot_row["parent_msg_id"]) if bot_row["parent_msg_id"] else None
        if not user_row:
            return

        # Strip buttons from old message while we work
        await interaction.message.edit(view=None)
        db.delete_message(interaction.message.id)

        channel_id = interaction.channel_id
        persona = ch_persona(channel_id)
        chain = _db_chain(user_row["parent_msg_id"])
        system = get_system_prompt(persona, channel_id)
        messages_payload = [{"role": "system", "content": system}] + chain + [{"role": "user", "content": user_row["content"]}]

        try:
            gen = llm.complete(messages=messages_payload, provider=current_provider, model=current_model, cfg=config, temperature=0.85)
            raw, new_msg = await stream_to_discord(gen, interaction.message)
            thinking, raw_rest = extract_thinking(raw)
            cleaned = clean_response(raw_rest)
            cleaned, board_image = extract_board(cleaned)
            db.save_message(new_msg.id, user_row["discord_msg_id"], channel_id, "assistant", cleaned or "*(no response)*")
            style = get_style(persona, load_persona_style(persona))
            thinking_display = format_thinking_spoiler(thinking)
            if style:
                chunks = chunk_text(cleaned, limit=EMBED_DESC_LIMIT)
                await new_msg.edit(content=thinking_display, embed=make_embed(chunks[0], style) if chunks else None, view=ResponseView())
                for extra in chunks[1:]:
                    await interaction.message.channel.send(embed=make_embed(extra, style))
            else:
                short_display = format_thinking_spoiler(thinking, limit=800)
                full_content = (short_display + "\n\n" + cleaned) if short_display and cleaned else (short_display or cleaned or "*(no response)*")
                chunks = chunk_text(full_content)
                await new_msg.edit(content=chunks[0] if chunks else "*(no response)*", view=ResponseView())
                for extra in chunks[1:]:
                    await interaction.message.channel.send(extra)
            if board_image:
                await interaction.message.channel.send(file=discord.File(io.BytesIO(board_image), filename="board.png"))
        except Exception as e:
            log.error("Regen (button) error: %s", e)

    @discord.ui.button(label="📌 pin", style=discord.ButtonStyle.secondary, custom_id="psychograph:pin")
    async def pin(self, interaction: discord.Interaction, button: discord.ui.Button):
        msg = interaction.message
        content = msg.embeds[0].description if msg.embeds else msg.content
        if content:
            db.add_pin(interaction.channel_id, content[:200])
            await interaction.response.send_message("-# *· pinned ·*", ephemeral=True)
        else:
            await interaction.response.send_message("-# *· nothing to pin ·*", ephemeral=True)


def _options_embed(channel_id: int) -> discord.Embed:
    """Build the settings overview embed, tinted with the current persona's colour."""
    persona = ch_persona(channel_id)
    verb = ch_verbosity(channel_id)
    style = get_style(persona, load_persona_style(persona))
    color = style["color"] if style else 0x2B2D31
    embed = discord.Embed(title="⚙️ settings", color=color)
    embed.add_field(name="persona", value=f"`{persona}`", inline=True)
    embed.add_field(name="verbosity", value=f"**{verb}/5**", inline=True)
    embed.add_field(name="provider", value=f"`{current_provider}`", inline=True)
    embed.add_field(name="model", value=f"`{current_model}`", inline=False)
    embed.set_footer(text=VERBOSITY_LABELS[verb])
    return embed


class PersonaSelect(discord.ui.Select):
    def __init__(self, channel_id: int):
        self.channel_id = channel_id
        names = list_personas()[:25]  # Discord select limit
        current = ch_persona(channel_id)
        options = [
            discord.SelectOption(label=n, value=n, default=(n == current))
            for n in names
        ]
        super().__init__(placeholder="switch persona…", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        name = self.values[0]
        if load_persona(name) is None:
            await interaction.response.send_message(f"Persona `{name}` not found.", ephemeral=True)
            return
        channel_id = interaction.channel_id
        if chess_engine.is_any_chess_persona(ch_persona(channel_id)):
            chess_engine.reset_game(channel_id)
        db.set_channel_persona(channel_id, name)
        db.clear_channel(channel_id)
        if chess_engine.is_any_chess_persona(name):
            chess_engine.reset_game(channel_id)
        await interaction.response.edit_message(embed=_options_embed(channel_id), view=OptionsView(channel_id))


class OptionsView(discord.ui.View):
    """Ephemeral settings panel with verbosity selector and persona switcher."""

    def __init__(self, channel_id: int):
        super().__init__(timeout=120)
        self.channel_id = channel_id
        self.add_item(PersonaSelect(channel_id))
        # Highlight the active verbosity button
        active = ch_verbosity(channel_id)
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.label.isdigit():
                child.style = discord.ButtonStyle.success if int(child.label) == active else discord.ButtonStyle.secondary

    def _verb_style(self, level: int) -> discord.ButtonStyle:
        return discord.ButtonStyle.success if ch_verbosity(self.channel_id) == level else discord.ButtonStyle.secondary

    @discord.ui.button(label="1", style=discord.ButtonStyle.secondary, custom_id="opt:v1", row=1)
    async def v1(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_verbosity(interaction, 1)

    @discord.ui.button(label="2", style=discord.ButtonStyle.secondary, custom_id="opt:v2", row=1)
    async def v2(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_verbosity(interaction, 2)

    @discord.ui.button(label="3", style=discord.ButtonStyle.secondary, custom_id="opt:v3", row=1)
    async def v3(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_verbosity(interaction, 3)

    @discord.ui.button(label="4", style=discord.ButtonStyle.secondary, custom_id="opt:v4", row=1)
    async def v4(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_verbosity(interaction, 4)

    @discord.ui.button(label="5", style=discord.ButtonStyle.secondary, custom_id="opt:v5", row=1)
    async def v5(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._set_verbosity(interaction, 5)

    async def _set_verbosity(self, interaction: discord.Interaction, level: int):
        db.set_channel_verbosity(interaction.channel_id, level)
        await interaction.response.edit_message(embed=_options_embed(interaction.channel_id), view=OptionsView(interaction.channel_id))

    @discord.ui.button(label="♻️ reset context", style=discord.ButtonStyle.danger, custom_id="opt:reset", row=2)
    async def reset_ctx(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel_id = interaction.channel_id
        db.clear_channel(channel_id)
        if chess_engine.is_any_chess_persona(ch_persona(channel_id)):
            chess_engine.reset_game(channel_id)
        await interaction.response.edit_message(embed=_options_embed(channel_id), view=OptionsView(channel_id))


def get_system_prompt(persona_name: str, channel_id: int) -> str:
    persona_text = load_persona(persona_name) or f"You are {persona_name}."
    pins = db.get_pins(channel_id)
    pin_section = "\n\n**Pinned notes:**\n" + "\n".join(f"- {p}" for p in pins) if pins else ""
    return persona_text + pin_section + _build_meta_suffix(ch_verbosity(channel_id))


def _build_meta_suffix(verbosity: int) -> str:
    timestamp = datetime.now().strftime("%A, %d %B %Y %H:%M")
    return (
        "\n\n---\n"
        f"**Current date/time:** {timestamp}\n\n"
        "## Runtime capabilities\n\n"
        "You have one tool: **web_search** — use it when you need current information "
        "you cannot answer reliably from training data. Prefer answering directly.\n\n"
        "**Discord formatting:** URLs render inline — articles auto-preview, "
        "images display in-chat. Link or embed when it adds something relevant.\n\n"
        f"## Response length — verbosity {verbosity}/5\n"
        f"{VERBOSITY_INSTRUCTIONS[verbosity]}"
    )


_BOARD_TAG = re.compile(r'\[board:\s*([^\]]+)\]', re.IGNORECASE)

_ARTIFACT_PATTERNS = [
    r"<tool_call>.*?</tool_call>",                          # Qwen, generic
    r"<tool_response>.*?</tool_response>",                   # Qwen, generic
    r"<function_calls>.*?</function_calls>",                 # some Claude fine-tunes
    r"\[TOOL_CALLS\].*?(?=\n\S|\Z)",                         # Mistral
    r"<\|python_tag\|>.*?(?:<\|eot_id\|>|\Z)",              # Llama 3.1+
    r"\[TOOL_REQUEST\].*?(?:\[END_TOOL_REQUEST\]|\Z)",       # Gemma-3 / LM Studio
    r'^\s*\{"name":\s*"(?:web_search|search)".*?\}\s*$',    # bare JSON function leakage
]


def extract_board(text: str) -> tuple[str, bytes | None]:
    """
    Strip [board: FEN] tag from text.
    Returns (text_without_tag, png_bytes) if Pillow is available,
    or (text_without_tag + ascii_board, None) as a fallback.
    """
    m = _BOARD_TAG.search(text)
    if not m:
        return text, None
    fen   = m.group(1).strip()
    clean = _BOARD_TAG.sub('', text).strip()
    image = fen_to_image(fen)
    if image is None:
        # Pillow unavailable — append ASCII board to the text response
        board_text = fen_to_board(fen)
        return (clean + '\n\n' + board_text).strip() if board_text else clean, None
    return clean, image


def extract_thinking(text: str) -> tuple[str, str]:
    """Extract <think>…</think> reasoning block.
    Returns (thinking_text, text_without_think_blocks).
    Handles both closed blocks and unclosed (model stopped mid-think)."""
    m = re.search(r"<think>(.*?)</think>", text, flags=re.DOTALL)
    if m:
        thinking = m.group(1).strip()
        rest = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        return thinking, rest
    m = re.search(r"<think>(.*)", text, flags=re.DOTALL)
    if m:
        thinking = m.group(1).strip()
        rest = re.sub(r"<think>.*", "", text, flags=re.DOTALL).strip()
        return thinking, rest
    return "", text


_THINKING_SPOILER_LIMIT = 1200   # max chars shown in the expandable spoiler


def format_thinking_spoiler(thinking: str, limit: int = _THINKING_SPOILER_LIMIT) -> str | None:
    """Wrap reasoning text in a Discord spoiler block, or return None if empty."""
    if not thinking:
        return None
    body = thinking[:limit]
    suffix = "\n-# *(truncated)*" if len(thinking) > limit else ""
    return f"-# 💭 *reasoning · click to expand*\n||{body}{suffix}||"


def clean_response(text: str) -> str:
    for pat in _ARTIFACT_PATTERNS:
        text = re.sub(pat, "", text, flags=re.DOTALL | re.MULTILINE)
    return text.strip()


def chunk_text(text: str, limit: int = DISCORD_MSG_LIMIT) -> list[str]:
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        split = text.rfind("\n", 0, limit) or text.rfind(" ", 0, limit) or limit
        chunks.append(text[:split])
        text = text[split:].lstrip()
    return chunks


def _db_chain(parent_id: int | None) -> list[dict]:
    """Walk DB parent links upward, return [{role, content}] in chronological order."""
    chain, visited = [], set()
    max_msgs = config.get("context", {}).get("max_messages", 40)
    while parent_id and len(chain) < max_msgs:
        if parent_id in visited:
            break
        visited.add(parent_id)
        row = db.get_message(parent_id)
        if not row:
            break
        chain.append({"role": row["role"], "content": row["content"]})
        parent_id = row["parent_msg_id"]
    chain.reverse()
    return chain


async def build_context(message: discord.Message, bot_id: int) -> list[dict]:
    """Walk reply chain (DB first, Discord API fallback) → chronological [{role, content}]."""
    max_msgs = config.get("context", {}).get("max_messages", 40)
    chain, visited = [], set()
    parent_id = message.reference.message_id if message.reference else None
    reset_ts = db.get_channel_reset_ts(message.channel.id)

    while parent_id and len(chain) < max_msgs:
        if parent_id in visited:
            break
        visited.add(parent_id)
        row = db.get_message(parent_id)
        if row:
            chain.append({"role": row["role"], "content": row["content"]})
            parent_id = row["parent_msg_id"]
        else:
            try:
                d_msg = await message.channel.fetch_message(parent_id)
                # Stop at any message that predates the last @bot reset — this prevents
                # cleared context from leaking back in via the Discord API fallback.
                if reset_ts and d_msg.created_at.timestamp() < reset_ts:
                    break
                role = "assistant" if d_msg.author.id == bot_id else "user"
                text = re.sub(rf"<@!?{bot_id}>", "", d_msg.content).strip() if role == "user" else d_msg.content
                chain.append({"role": role, "content": text})
                parent_id = d_msg.reference.message_id if d_msg.reference else None
            except (discord.NotFound, discord.HTTPException):
                break

    chain.reverse()
    return chain


async def stream_to_discord(gen, reply_target: discord.Message) -> tuple[str, discord.Message]:
    placeholder = await reply_target.reply(random.choice(_THINKING_LINES))
    full_text, last_edit = "", 0.0
    try:
        async for chunk in gen:
            full_text += chunk
            now = asyncio.get_event_loop().time()
            if now - last_edit >= STREAM_EDIT_INTERVAL:
                # Strip <think> blocks from the live view — the placeholder stays as
                # "thinking…" while the model reasons, then streams the actual response.
                display = re.sub(r"<think>.*?</think>", "", full_text, flags=re.DOTALL)
                display = re.sub(r"<think>.*", "", display, flags=re.DOTALL).strip()
                if display:
                    try:
                        await placeholder.edit(content=display[:DISCORD_MSG_LIMIT])
                        last_edit = now
                    except discord.HTTPException:
                        pass
    except Exception:
        try:
            await placeholder.delete()
        except discord.HTTPException:
            pass
        raise
    return full_text, placeholder


intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)


async def do_restart() -> None:
    await asyncio.sleep(0.5)
    if _INSTANCE_LOCK:
        _INSTANCE_LOCK.close()
    subprocess.Popen([sys.executable] + sys.argv)
    await bot.close()


@bot.event
async def on_ready():
    db.init_db()
    bot.add_view(ResponseView())  # re-register persistent view after restart
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    if current_provider == "openrouter" and not os.getenv("OPENROUTER_API_KEY"):
        log.error("OPENROUTER_API_KEY is not set in .env — OpenRouter requests will fail with 401")
    log.info("Provider: %s  Model: %s  Default persona: %s", current_provider, current_model, config.get("persona", "mochi"))
    log.info("Ready — mention the bot to chat.")


async def handle_command(message: discord.Message, cmd: str, *, _out: list[str] | None = None) -> bool:
    global current_provider, current_model, chess_level

    cmd_lower = cmd.lower().strip()

    async def emit(text: str) -> None:
        """Buffer text when chaining commands, otherwise send immediately."""
        if _out is not None:
            _out.append(text)
        else:
            await message.reply(text)

    if cmd_lower == "resign":
        if not chess_engine.is_any_chess_persona(ch_persona(message.channel.id)):
            await emit("· no game in progress · no king to topple ·")
            return True
        fen = chess_engine.current_fen(message.channel.id)
        chess_engine.reset_game(message.channel.id)
        text, board_image = extract_board(f"{random.choice(_RESIGN_MSGS)}\n\n[board: {fen}]")
        await emit(text)
        if board_image:
            await message.channel.send(file=discord.File(io.BytesIO(board_image), filename="board.png"))
        return True

    if cmd_lower == "reset":
        db.clear_channel(message.channel.id)
        if chess_engine.is_any_chess_persona(ch_persona(message.channel.id)):
            chess_engine.reset_game(message.channel.id)
        await emit("·˚ slate wiped · starting fresh ˚·")
        return True

    if cmd_lower == "personas":
        names = list_personas()
        lines = [f"→ {n} *(active)*" if n == ch_persona(message.channel.id) else f"  {n}" for n in names]
        await emit("· ★ · personas · ★ ·\n```\n" + "\n".join(lines) + "\n```")
        return True

    if cmd_lower.startswith("persona "):
        name = cmd[8:].strip().lower().replace(" ", "_")
        if load_persona(name) is None:
            await emit(f"Persona `{name}` not found. Available: {', '.join(list_personas())}")
            return True
        if chess_engine.is_any_chess_persona(ch_persona(message.channel.id)):
            chess_engine.reset_game(message.channel.id)
        db.set_channel_persona(message.channel.id, name)
        db.clear_channel(message.channel.id)
        if chess_engine.is_any_chess_persona(name):
            chess_engine.reset_game(message.channel.id)
        await emit(f"· now speaking as: **{name}** · previous context dissolved ·")
        return True

    if cmd_lower == "prompt":
        active = ch_persona(message.channel.id)
        text = load_persona(active) or f"(no persona file for {active})"
        for chunk in chunk_text(f"**Active persona:** `{active}`\n\n" + text):
            await emit(chunk)
        return True

    if cmd_lower.startswith("verbosity "):
        val = cmd[10:].strip()
        if val.isdigit() and 1 <= int(val) <= 5:
            level = int(val)
            db.set_channel_verbosity(message.channel.id, level)
            await emit(f"· verbosity **{level}/5** · {VERBOSITY_LABELS[level]}")
        else:
            await emit("· usage: `verbosity <1-5>` ·")
        return True

    if cmd_lower == "level":
        depth, elo = CHESS_LEVEL_MAP[chess_level]
        await emit(f"· stockfish · **level {chess_level}/8** · {elo} Elo · depth {depth} ·")
        return True

    if cmd_lower.startswith("level "):
        val = cmd[6:].strip()
        if val.isdigit() and 1 <= int(val) <= 8:
            chess_level = int(val)
            depth, elo = CHESS_LEVEL_MAP[chess_level]
            await emit(f"· difficulty dialled to **level {chess_level}/8** · {elo} Elo · depth {depth} ·")
        else:
            await emit(
                "· ♟ · level <1-8> · ♟ ·\n```\n"
                + "\n".join(f"  {l}  depth {d:>2}  {e}" for l, (d, e) in CHESS_LEVEL_MAP.items())
                + "\n```"
            )
        return True

    if cmd_lower == "model":
        if current_provider == "local":
            models = await llm.get_local_models(config)
            body = "\n".join(f"→ **{m}** *(active)*" if m == current_model else f"  {m}" for m in models) if models else "Could not reach LM Studio or no models loaded."
            await emit(f"**Local models:**\n```\n{body}\n```" if models else body)
        else:
            models = config["providers"].get(current_provider, {}).get("models", [])
            lines = [f"→ **{m}** *(active)*" if m == current_model else f"  {m}" for m in models]
            await emit(f"**{current_provider} models:**\n```\n" + "\n".join(lines) + "\n```")
        return True

    if cmd_lower == "model random":
        if current_provider != "openrouter":
            await emit("Random model selection is only available for the OpenRouter provider.")
            return True
        models = await llm.get_openrouter_models(config, paid_only=True)
        if not models:
            await emit("Could not fetch models from OpenRouter (check your API key or network).")
            return True
        current_model = random.choice(models)
        config["default_model"] = current_model
        save_config(config)
        await emit(f"🎲 Random model: **{current_model}**")
        return True

    if cmd_lower in ("model free random", "model random free"):
        if current_provider != "openrouter":
            await emit("Random free model selection is only available for the OpenRouter provider.")
            return True
        models = await llm.get_openrouter_models(config, free_only=True)
        if not models:
            await emit("Could not fetch free models from OpenRouter (check your API key or network).")
            return True
        current_model = random.choice(models)
        config["default_model"] = current_model
        save_config(config)
        await emit(f"🎲 Random free model: **{current_model}**")
        return True

    if cmd_lower == "model free":
        if current_provider != "openrouter":
            await emit("Free model listing is only available for the OpenRouter provider.")
            return True
        models = await llm.get_openrouter_models(config, free_only=True)
        if not models:
            await emit("Could not fetch free models from OpenRouter (check your API key or network).")
            return True
        lines = [f"→ **{m}** *(active)*" if m == current_model else f"  {m}" for m in models]
        body = "\n".join(lines)
        for chunk in chunk_text(f"**OpenRouter free models:**\n```\n{body}\n```"):
            await emit(chunk)
        return True

    if cmd_lower.startswith("model "):
        current_model = cmd[6:].strip()
        config["default_model"] = current_model
        save_config(config)
        await emit(f"Model switched to **{current_model}**.")
        return True

    if cmd_lower.startswith("provider "):
        name = cmd[9:].strip().lower()
        if name not in config.get("providers", {}):
            await emit(f"Unknown provider `{name}`. Available: {', '.join(config.get('providers', {}).keys())}")
            return True
        current_provider = name
        config["default_provider"] = name
        if name == "local":
            models = await llm.get_local_models(config)
            current_model = models[0] if models else "local-model"
        else:
            models = config["providers"][name].get("models", [])
            current_model = models[0] if models else "unknown"
        config["default_model"] = current_model
        save_config(config)
        await emit(f"Switched to **{name}** provider, model **{current_model}**.")
        return True

    if cmd_lower == "restart":
        await emit("·˚ rebooting · back in a moment ˚·")
        asyncio.create_task(do_restart())
        return True

    if cmd_lower == "options":
        # options always sends directly (has a View, can't be buffered)
        await message.reply(embed=_options_embed(message.channel.id), view=OptionsView(message.channel.id))
        return True

    return False


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if not (isinstance(message.channel, discord.DMChannel) or bot.user in message.mentions):
        return

    prompt = re.sub(rf"<@!?{bot.user.id}>", "", message.content).strip()

    if not prompt and not message.attachments:
        await message.reply("You mentioned me but didn't say anything!")
        return

    parts = [p.strip() for p in prompt.split(";") if p.strip()]
    if len(parts) > 1:
        out: list[str] = []
        if await handle_command(message, parts[0], _out=out):
            for part in parts[1:]:
                if not await handle_command(message, part, _out=out):
                    out.append(f"*(unknown command: `{part}`)*")
            if out:
                await message.reply("\n\n".join(out))
            return

    if prompt and await handle_command(message, prompt):
        return

    persona = ch_persona(message.channel.id)
    is_chess = chess_engine.is_chess_persona(persona)
    is_chess_classic = chess_engine.is_chess_classic_persona(persona)

    # ── Chess: validate user move before calling the LLM ─────────
    if is_chess and prompt:
        ok, san_or_err, fen = chess_engine.apply_user_move(message.channel.id, prompt)
        if not ok:
            await message.reply(san_or_err)
            return

    # ── Chess-classic: Stockfish via chess-api.com, no LLM ───────
    if is_chess_classic:
        channel_id = message.channel.id
        parent_id = message.reference.message_id if message.reference else None

        async def _reply_classic(text: str) -> None:
            """Send a chess-classic reply, save both sides to DB."""
            db.save_message(message.id, parent_id, channel_id, "user", prompt)
            clean, board_image = extract_board(text)
            try:
                reply_msg = await message.reply(clean)
            except discord.HTTPException as e:
                log.error("chess-classic send failed: %s", e)
                return
            db.save_message(reply_msg.id, message.id, channel_id, "assistant", clean)
            if board_image:
                try:
                    await message.channel.send(
                        file=discord.File(io.BytesIO(board_image), filename="board.png")
                    )
                except discord.HTTPException as e:
                    log.error("chess-classic board image send failed: %s", e)

        # 0. Game already over from a previous move?
        status = chess_engine.game_status(channel_id)
        if status:
            await _reply_classic(
                f"{_chess_result_text(status)}\n\n[board: {chess_engine.current_fen(channel_id)}]"
            )
            return

        # 1. No move text → show current board
        if not prompt:
            await _reply_classic(
                f"· ♟ · awaiting your move · SAN or UCI · ♟ ·\n\n"
                f"[board: {chess_engine.current_fen(channel_id)}]"
            )
            return

        # 2. Apply the user's move (validates + persists)
        ok, san_or_err, _ = chess_engine.apply_user_move(channel_id, prompt)
        if not ok:
            await message.reply(san_or_err)
            return

        # 3. Game over after user's move?
        status = chess_engine.game_status(channel_id)
        if status:
            await _reply_classic(
                f"{_chess_result_text(status)}\n\n[board: {chess_engine.current_fen(channel_id)}]"
            )
            return

        # 4. Ask Stockfish for the bot's reply
        depth, _ = CHESS_LEVEL_MAP[chess_level]
        result = await chess_api.get_stockfish_move(chess_engine.current_fen(channel_id), depth=depth)
        if result is None:
            await message.reply("·˚ the oracle is silent · chess-api.com unreachable · try again ˚·")
            return

        # 5. Apply the bot's move (validates + persists)
        ok, san_or_err, _ = chess_engine.apply_bot_move(channel_id, result["move"])
        if not ok:
            log.error("Stockfish returned illegal move: %s", result.get("move"))
            await message.reply(
                f"Engine error — Stockfish returned an illegal move (`{result.get('move')}`). "
                "Use `@bot reset` to start over."
            )
            return

        # 6. Bot SAN + optional game-over line + board image
        status = chess_engine.game_status(channel_id)
        reply_text = f"**{result['san']}**"
        if status:
            reply_text += f"\n\n{_chess_result_text(status)}"
        reply_text += f"\n\n[board: {chess_engine.current_fen(channel_id)}]"

        await _reply_classic(reply_text)
        return

    system = get_system_prompt(persona, message.channel.id)

    # Inject authoritative board state for chess persona
    if is_chess:
        fen_now = chess_engine.current_fen(message.channel.id)
        status = chess_engine.game_status(message.channel.id)
        chess_ctx = (
            f"\n\n## Board state (authoritative — maintained by the engine)\n"
            f"FEN: `{fen_now}`\n"
            f"Move: {chess_engine.move_number(message.channel.id)}, "
            f"{chess_engine.side_to_move(message.channel.id)} to move\n"
            f"Legal moves: {chess_engine.legal_moves_str(message.channel.id)}"
        )
        if status:
            chess_ctx += f"\n**Game over: {status}**"
        system += chess_ctx

    context = await build_context(message, bot.user.id)

    image_blocks = await llm.format_image_blocks(message.attachments)
    user_content = ([{"type": "text", "text": prompt or " "}] + image_blocks) if image_blocks else (prompt or " ")

    messages_payload = [{"role": "system", "content": system}] + context + [{"role": "user", "content": user_content}]

    parent_id = message.reference.message_id if message.reference else None
    db.save_message(message.id, parent_id, message.channel.id, "user", prompt)

    async with message.channel.typing():
        try:
            gen = llm.complete(messages=messages_payload, provider=current_provider, model=current_model, cfg=config)
            raw, reply_msg = await stream_to_discord(gen, message)
        except Exception as e:
            log.error("LLM error: %s", e)
            await message.reply(f"Error: {e}")
            db.delete_message(message.id)
            return

    thinking, raw_rest = extract_thinking(raw)
    cleaned = clean_response(raw_rest)

    # ── Chess: validate LLM move, retry if illegal ───────────────
    if is_chess and not chess_engine.game_status(message.channel.id):
        bot_move_token = chess_engine.extract_bot_move(cleaned)
        retries = 0
        while retries < MAX_CHESS_RETRIES:
            if bot_move_token:
                ok, san_or_err, fen = chess_engine.apply_bot_move(message.channel.id, bot_move_token)
                if ok:
                    # Replace any [board: ...] tag with the authoritative FEN
                    cleaned = _BOARD_TAG.sub("", cleaned).strip()
                    cleaned += f"\n\n[board: {fen}]"
                    break
            # Illegal or unparseable — retry with feedback
            retries += 1
            log.warning("Chess retry %d/%d — illegal move '%s'", retries, MAX_CHESS_RETRIES, bot_move_token)
            legal = chess_engine.legal_moves_str(message.channel.id)
            retry_msg = (
                f"Your move '{bot_move_token}' is illegal. "
                f"The current FEN is: {chess_engine.current_fen(message.channel.id)}\n"
                f"Legal moves: {legal}\n"
                f"Pick one legal move and respond with ONLY that move in SAN."
            )
            retry_payload = messages_payload + [
                {"role": "assistant", "content": cleaned},
                {"role": "user", "content": retry_msg},
            ]
            try:
                retry_gen = llm.complete(messages=retry_payload, provider=current_provider, model=current_model, cfg=config)
                retry_raw = ""
                async for chunk in retry_gen:
                    retry_raw += chunk
                _, retry_rest = extract_thinking(retry_raw)
                cleaned = clean_response(retry_rest)
                bot_move_token = chess_engine.extract_bot_move(cleaned)
            except Exception as e:
                log.error("Chess retry LLM error: %s", e)
                break
        else:
            # All retries exhausted
            cleaned = f"*(Failed to produce a legal move after {MAX_CHESS_RETRIES} attempts. Use 🔄 to regenerate.)*"

    cleaned, board_image = extract_board(cleaned)
    if not cleaned and not board_image:
        await reply_msg.edit(content="*(no response)*")
        db.delete_message(message.id)
        return

    db.save_message(reply_msg.id, message.id, message.channel.id, "assistant", cleaned)
    style = get_style(persona, load_persona_style(persona))
    is_chess_persona = chess_engine.is_any_chess_persona(persona)
    response_view = ResponseView() if not is_chess_persona else None
    thinking_display = format_thinking_spoiler(thinking)
    if style:
        # Embed responses: thinking goes in `content` (above the embed), response in description.
        chunks = chunk_text(cleaned, limit=EMBED_DESC_LIMIT)
        await reply_msg.edit(content=thinking_display, embed=make_embed(chunks[0], style) if chunks else None, view=response_view)
        for extra in chunks[1:]:
            await message.channel.send(embed=make_embed(extra, style))
    else:
        # Plain text: prepend a shorter spoiler so both fit within the message limit.
        short_display = format_thinking_spoiler(thinking, limit=800)
        full_content = (short_display + "\n\n" + cleaned) if short_display and cleaned else (short_display or cleaned or "-# *…*")
        chunks = chunk_text(full_content)
        await reply_msg.edit(content=chunks[0] if chunks else "-# *…*", view=response_view)
        for extra in chunks[1:]:
            await message.channel.send(extra)
    if board_image:
        await message.channel.send(file=discord.File(io.BytesIO(board_image), filename="board.png"))


@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User):
    if user.bot or reaction.message.author != bot.user:
        return

    channel_id = reaction.message.channel.id

    if str(reaction.emoji) == "🔄":
        bot_row = db.get_message(reaction.message.id)
        if not bot_row:
            return
        user_row = db.get_message(bot_row["parent_msg_id"]) if bot_row["parent_msg_id"] else None
        if not user_row:
            return

        db.delete_message(reaction.message.id)
        persona = ch_persona(channel_id)
        chain = _db_chain(user_row["parent_msg_id"])
        system = get_system_prompt(persona, channel_id)
        messages_payload = [{"role": "system", "content": system}] + chain + [{"role": "user", "content": user_row["content"]}]

        try:
            async with reaction.message.channel.typing():
                gen = llm.complete(messages=messages_payload, provider=current_provider, model=current_model, cfg=config, temperature=0.85)
                raw, new_msg = await stream_to_discord(gen, reaction.message)
            thinking, raw_rest = extract_thinking(raw)
            cleaned = clean_response(raw_rest)
            cleaned, board_image = extract_board(cleaned)
            db.save_message(new_msg.id, user_row["discord_msg_id"], channel_id, "assistant", cleaned or "*(no response)*")
            style = get_style(persona, load_persona_style(persona))
            response_view = ResponseView() if not chess_engine.is_any_chess_persona(persona) else None
            thinking_display = format_thinking_spoiler(thinking)
            if style:
                chunks = chunk_text(cleaned, limit=EMBED_DESC_LIMIT)
                await new_msg.edit(content=thinking_display, embed=make_embed(chunks[0], style) if chunks else None, view=response_view)
                for extra in chunks[1:]:
                    await reaction.message.channel.send(embed=make_embed(extra, style))
            else:
                short_display = format_thinking_spoiler(thinking, limit=800)
                full_content = (short_display + "\n\n" + cleaned) if short_display and cleaned else (short_display or cleaned or "*(no response)*")
                chunks = chunk_text(full_content)
                await new_msg.edit(content=chunks[0] if chunks else "*(no response)*", view=response_view)
                for extra in chunks[1:]:
                    await reaction.message.channel.send(extra)
            if board_image:
                await reaction.message.channel.send(file=discord.File(io.BytesIO(board_image), filename="board.png"))
        except Exception as e:
            log.error("Regen error: %s", e)

    elif str(reaction.emoji) == "📌":
        if not reaction.message.content:
            return
        try:
            db.add_pin(channel_id, reaction.message.content[:200])
            await reaction.message.add_reaction("✅")
            log.info("Pinned note for channel %d", channel_id)
        except Exception as e:
            log.error("Pin error: %s", e)


if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise RuntimeError("DISCORD_TOKEN not set in .env")
    bot.run(token)
