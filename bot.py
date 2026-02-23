import asyncio
import logging
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
current_persona: str = config.get("persona", "pineapple")
verbosity: int = 2

DISCORD_MSG_LIMIT = 1990
STREAM_EDIT_INTERVAL = 1.2

VERBOSITY_INSTRUCTIONS = {
    1: "ONE sentence. Stop after the period. No lists, no follow-up thoughts, no elaboration.",
    2: "1-3 sentences, no more. No bullet points, no preamble. Cut anything that isn't the core response.",
    3: "One short paragraph. Make the point, add one supporting thought, stop.",
    4: "A full paragraph. Be substantive and thorough.",
    5: "No length limit. Full depth, full character voice — as long as the response warrants.",
}

import db
import llm
from personas import list_personas, load_persona


def get_system_prompt(persona_name: str, channel_id: int) -> str:
    persona_text = load_persona(persona_name) or f"You are {persona_name}."
    pins = db.get_pins(channel_id)
    pin_section = "\n\n**Pinned notes:**\n" + "\n".join(f"- {p}" for p in pins) if pins else ""
    return persona_text + pin_section + _build_meta_suffix()


def _build_meta_suffix() -> str:
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


_ARTIFACT_PATTERNS = [
    r"<tool_call>.*?</tool_call>",                          # Qwen, generic
    r"<tool_response>.*?</tool_response>",                   # Qwen, generic
    r"<function_calls>.*?</function_calls>",                 # some Claude fine-tunes
    r"\[TOOL_CALLS\].*?(?=\n\S|\Z)",                         # Mistral
    r"<\|python_tag\|>.*?(?:<\|eot_id\|>|\Z)",              # Llama 3.1+
    r"\[TOOL_REQUEST\].*?(?:\[END_TOOL_REQUEST\]|\Z)",       # Gemma-3 / LM Studio
    r'^\s*\{"name":\s*"(?:web_search|search)".*?\}\s*$',    # bare JSON function leakage
]


def clean_response(text: str) -> str:
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    text = re.sub(r"<think>.*", "", text, flags=re.DOTALL).strip()
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
                role = "assistant" if d_msg.author.id == bot_id else "user"
                text = re.sub(rf"<@!?{bot_id}>", "", d_msg.content).strip() if role == "user" else d_msg.content
                chain.append({"role": role, "content": text})
                parent_id = d_msg.reference.message_id if d_msg.reference else None
            except (discord.NotFound, discord.HTTPException):
                break

    chain.reverse()
    return chain


async def stream_to_discord(gen, reply_target: discord.Message) -> tuple[str, discord.Message]:
    placeholder = await reply_target.reply("-# *generating...*")
    full_text, last_edit = "", 0.0
    try:
        async for chunk in gen:
            full_text += chunk
            now = asyncio.get_event_loop().time()
            if now - last_edit >= STREAM_EDIT_INTERVAL and full_text.strip():
                try:
                    await placeholder.edit(content=full_text[:DISCORD_MSG_LIMIT])
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
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id)
    log.info("Provider: %s  Model: %s  Persona: %s", current_provider, current_model, current_persona)
    log.info("Ready — mention the bot to chat.")


async def handle_command(message: discord.Message, cmd: str) -> bool:
    global current_persona, current_provider, current_model, verbosity

    cmd_lower = cmd.lower().strip()

    if cmd_lower == "reset":
        db.clear_channel(message.channel.id)
        await message.reply("Context cleared.")
        return True

    if cmd_lower == "personas":
        names = list_personas()
        lines = [f"→ **{n}** *(active)*" if n == current_persona else f"  {n}" for n in names]
        await message.reply("**Personas:**\n```\n" + "\n".join(lines) + "\n```")
        return True

    if cmd_lower.startswith("persona "):
        name = cmd[8:].strip().lower().replace(" ", "_")
        if load_persona(name) is None:
            await message.reply(f"Persona `{name}` not found. Available: {', '.join(list_personas())}")
            return True
        current_persona = name
        config["persona"] = name
        save_config(config)
        db.clear_channel(message.channel.id)
        await message.reply(f"Persona switched to **{name}**. Context cleared.")
        return True

    if cmd_lower == "prompt":
        text = load_persona(current_persona) or f"(no persona file for {current_persona})"
        for chunk in chunk_text(f"**Active persona:** `{current_persona}`\n\n" + text):
            await message.reply(chunk)
        return True

    if cmd_lower.startswith("verbosity "):
        val = cmd[10:].strip()
        if val.isdigit() and 1 <= int(val) <= 5:
            verbosity = int(val)
            await message.reply(f"Verbosity set to **{verbosity}/5** — {VERBOSITY_INSTRUCTIONS[verbosity]}")
        else:
            await message.reply("Usage: `verbosity <1-5>`")
        return True

    if cmd_lower == "model":
        if current_provider == "local":
            models = await llm.get_local_models(config)
            body = "\n".join(f"→ **{m}** *(active)*" if m == current_model else f"  {m}" for m in models) if models else "Could not reach LM Studio or no models loaded."
            await message.reply(f"**Local models:**\n```\n{body}\n```" if models else body)
        else:
            models = config["providers"].get(current_provider, {}).get("models", [])
            lines = [f"→ **{m}** *(active)*" if m == current_model else f"  {m}" for m in models]
            await message.reply(f"**{current_provider} models:**\n```\n" + "\n".join(lines) + "\n```")
        return True

    if cmd_lower.startswith("model "):
        current_model = cmd[6:].strip()
        config["default_model"] = current_model
        save_config(config)
        await message.reply(f"Model switched to **{current_model}**.")
        return True

    if cmd_lower.startswith("provider "):
        name = cmd[9:].strip().lower()
        if name not in config.get("providers", {}):
            await message.reply(f"Unknown provider `{name}`. Available: {', '.join(config.get('providers', {}).keys())}")
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
        await message.reply(f"Switched to **{name}** provider, model **{current_model}**.")
        return True

    if cmd_lower == "restart":
        await message.reply("Restarting...")
        asyncio.create_task(do_restart())
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
    if len(parts) > 1 and await handle_command(message, parts[0]):
        for part in parts[1:]:
            if not await handle_command(message, part):
                await message.reply(f"*(unknown command: `{part}`)*")
        return

    if prompt and await handle_command(message, prompt):
        return

    system = get_system_prompt(current_persona, message.channel.id)
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

    cleaned = clean_response(raw)
    if not cleaned:
        await reply_msg.edit(content="*(no response)*")
        db.delete_message(message.id)
        return

    db.save_message(reply_msg.id, message.id, message.channel.id, "assistant", cleaned)
    chunks = chunk_text(cleaned)
    await reply_msg.edit(content=chunks[0])
    for extra in chunks[1:]:
        await message.channel.send(extra)


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
        chain = _db_chain(user_row["parent_msg_id"])
        system = get_system_prompt(current_persona, channel_id)
        messages_payload = [{"role": "system", "content": system}] + chain + [{"role": "user", "content": user_row["content"]}]

        try:
            async with reaction.message.channel.typing():
                gen = llm.complete(messages=messages_payload, provider=current_provider, model=current_model, cfg=config, temperature=0.85)
                raw, new_msg = await stream_to_discord(gen, reaction.message)
            cleaned = clean_response(raw)
            db.save_message(new_msg.id, user_row["discord_msg_id"], channel_id, "assistant", cleaned or "*(no response)*")
            chunks = chunk_text(cleaned)
            await new_msg.edit(content=chunks[0] if chunks else "*(no response)*")
            for extra in chunks[1:]:
                await reaction.message.channel.send(extra)
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
