import asyncio
import contextlib
import io
import logging
import random
import os
import re
import socket
import subprocess
import sys
import warnings
import time
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands, tasks
from discord import app_commands
import yaml
from dotenv import load_dotenv

import db
import llm
import chess_api
import chess_engine
from avatar_gen import generate_avatar
from board import fen_to_board, fen_to_image
from personas import list_personas, load_persona, load_persona_style, get_persona_metadata
from styles import get_style, make_embed, EMBED_DESC_LIMIT, VERBOSITY_LABELS
from ui import ResponseView, OptionsView, _options_embed, _get_options_view
from config_util import config, save_config

warnings.filterwarnings("ignore", message="Impersonate.*does not exist")
load_dotenv()

# Global lock manager for sequential vs parallel LLM generation
_PROVIDER_LOCKS = {"local": asyncio.Lock()}

def get_llm_lock(provider: str) -> asyncio.Lock | None:
    """Return a lock for sequential providers, or None for parallel ones."""
    if provider == "local":
        return _PROVIDER_LOCKS["local"]
    return None  # Parallel by default for cloud providers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("bot")

# Singleton guard placeholder
_INSTANCE_LOCK: socket.socket | None = None
SINGLETON_PORT = 47823

class PsychographBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.current_provider = config.get("default_provider", "local")
        self.current_model = config.get("default_model", "local-model")
        self.chess_level = 3

    async def setup_hook(self):
        db.init_db()
        self.add_view(ResponseView(bot_callback=self.handle_view_interaction))
        self.heartbeat.start()
        log.info("Views registered and Heartbeat started. Use !sync in a channel to update slash commands.")

    @tasks.loop(minutes=30)
    async def heartbeat(self):
        """Select a random persona to post in #sim-city once or twice a day."""
        now = time.time()
        last_run = db.get_last_run("sim_city_heartbeat")
        # Run every 12 hours approx (43200 seconds)
        if now - last_run < 40000:
            return

        for guild in self.guilds:
            channel = discord.utils.get(guild.text_channels, name="sim-city")
            if channel:
                persona_name = random.choice(list_personas())
                log.info(f"Heartbeat: {persona_name} is posting in #sim-city")

                # Simple prompt for heartbeat
                topics = ["the weather", "a random thought", "something you noticed today", "a dream you had", "a piece of news"]
                prompt = f"Write a short, characterful post about {random.choice(topics)}."

                system = get_system_prompt(persona_name, channel.id)
                messages = [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ]

                await process_llm_request(channel, messages, persona_name, None)
                db.set_last_run("sim_city_heartbeat", now)
                break

    async def get_or_create_webhook(self, channel: discord.TextChannel) -> discord.Webhook | None:
        if not isinstance(channel, discord.TextChannel):
            return None

        cached = db.get_channel_webhook(channel.id)
        if cached:
            try:
                return discord.Webhook.from_url(cached['webhook_url'], client=self)
            except:
                pass

        # Check existing webhooks in the channel
        try:
            webhooks = await channel.webhooks()
            for wh in webhooks:
                if wh.name == "SimCity Webhook":
                    db.save_channel_webhook(channel.id, wh.url, wh.id)
                    return wh

            # Create new one if not found
            wh = await channel.create_webhook(name="SimCity Webhook")
            db.save_channel_webhook(channel.id, wh.url, wh.id)
            return wh
        except Exception as e:
            log.error(f"Failed to get/create webhook: {e}")
            return None

    async def handle_view_interaction(self, interaction: discord.Interaction, action: str):
        if action == "regen":
            await self.handle_regen(interaction)

    async def handle_regen(self, interaction: discord.Interaction):
        await interaction.response.defer()
        bot_row = db.get_message(interaction.message.id)
        if not bot_row: return
        user_row = db.get_message(bot_row["parent_msg_id"]) if bot_row["parent_msg_id"] else None
        if not user_row: return

        await interaction.message.edit(view=None)
        db.delete_message(interaction.message.id)

        channel_id = interaction.channel_id
        persona = ch_persona(channel_id)
        temp = db.get_channel_temperature(channel_id)
        chain = _db_chain(user_row["parent_msg_id"])

        # Resolve mentions for the history chain + the user's message we are regening
        mentions_map = resolve_mentions(chain + [user_row], interaction.guild)
        # Ensure user_row's author is in the map with the correct tag
        if user_row.get("author_id"):
            uid = user_row["author_id"]
            mentions_map[str(uid)] = {"tag": f"<@{uid}>", "last_msg_id": user_row["discord_msg_id"]}

        system = get_system_prompt(persona, channel_id, mentions_map=mentions_map)
        messages_payload = [{"role": "system", "content": system}] + chain + [{"role": "user", "content": user_row["content"]}]

        await process_llm_request(interaction.message.channel, messages_payload, persona, user_row["discord_msg_id"],
                                  reply_to=interaction.message, temperature=temp, mentions_map=mentions_map)

bot = PsychographBot()

@bot.command()
@commands.is_owner()
async def sync(ctx: commands.Context):
    """Sync slash commands to the current guild immediately."""
    try:
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)
        await ctx.send(f"· synced {len(synced)} commands to this guild ·")
    except Exception as e:
        await ctx.send(f"Sync failed: {e}")

# Helper accessors
def ch_persona(cid: int) -> str: return db.get_channel_persona(cid) or config.get("persona", "mochi")
def ch_verbosity(cid: int) -> int: return db.get_channel_verbosity(cid)

# ── Formatting & Extraction ──────────────────────────────────────────────────

VERBOSITY_INSTRUCTIONS = {
    1: "ONE sentence. Stop after the period. No lists, no follow-up thoughts, no elaboration.",
    2: "1-3 sentences, no more. No bullet points, no preamble. Cut anything that isn't the core response.",
    3: "One short paragraph. Make the point, add one supporting thought, stop.",
    4: "A full paragraph. Be substantive and thorough.",
    5: "No length limit. Full depth, full character voice — as long as the response warrants.",
}

_THINKING_SIGNAL = "-# *· ✦ ·*"

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
    s = status.lower()
    if "white wins" in s: return random.choice(_WIN_MSGS)
    if "black wins" in s: return random.choice(_LOSS_MSGS)
    return random.choice(_DRAW_MSGS)

def get_system_prompt(persona_name: str, channel_id: int, mentions_map: dict = None) -> str:
    persona_text = load_persona(persona_name) or f"You are {persona_name}."
    pins = db.get_pins(channel_id)
    pin_section = "\n\n**Pinned notes:**\n" + "\n".join(f"- {p}" for p in pins) if pins else ""

    who_is_who = ""
    if mentions_map:
        lines = [f"- {name}: {tag}" for name, tag in mentions_map.items()]
        who_is_who = "\n\n## Users in this thread:\n" + "\n".join(lines) + "\n\nTo mention a user so they get a notification, you MUST use their <@ID> tag exactly as shown above. If you just use their name, they won't be notified."

    timestamp = datetime.now().strftime("%A, %d %B %Y %H:%M")
    verb = ch_verbosity(channel_id)
    meta = (
        "\n\n---\n"
        f"**Your name for this session:** {persona_name}\n"
        f"**Current date/time:** {timestamp}\n\n"
        "## Runtime capabilities\n\n"
        "You have one tool: **web_search** — use it when you need current information.\n\n"
        f"## Response length — verbosity {verb}/5\n"
        f"{VERBOSITY_INSTRUCTIONS[verb]}"
    )
    return persona_text + pin_section + who_is_who + meta

def resolve_mentions(chain: list[dict], guild: discord.Guild | None) -> dict[str, dict]:
    """Scans message history for Discord tags and resolves them to names/msg_ids."""
    mapping = {}
    if not guild: return mapping

    mention_re = re.compile(r"<@!?(\d+)>")
    for msg in chain:
        content = msg.get("content", "")
        if not isinstance(content, str): continue
        for match in mention_re.finditer(content):
            uid = int(match.group(1))
            member = guild.get_member(uid)
            if member:
                data = {"tag": f"<@{uid}>", "last_msg_id": msg.get("discord_msg_id")}
                mapping[member.display_name] = data
                mapping[member.name] = data

        # Also track by author_id directly from the chain
        if msg.get("author_id"):
            uid = msg["author_id"]
            member = guild.get_member(uid)
            if member:
                data = {"tag": f"<@{uid}>", "last_msg_id": msg["discord_msg_id"]}
                mapping[member.display_name] = data
                mapping[member.name] = data
                mapping[str(uid)] = data

    return mapping

def extract_thinking(text: str) -> tuple[str, str]:
    m = re.search(r"<think>(.*?)</think>", text, flags=re.DOTALL)
    if m:
        return m.group(1).strip(), re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    m = re.search(r"<think>(.*)", text, flags=re.DOTALL)
    if m:
        return m.group(1).strip(), re.sub(r"<think>.*", "", text, flags=re.DOTALL).strip()
    return "", text

def format_thinking_spoiler(thinking: str, limit: int = 1200) -> str | None:
    if not thinking: return None
    body = thinking[:limit]
    suffix = "\n-# *(truncated)*" if len(thinking) > limit else ""
    return f"-# 💭 *reasoning · click to expand*\n||{body}{suffix}||"

_BOARD_TAG = re.compile(r'\[board:\s*([^\]]+)\]', re.IGNORECASE)

def extract_board(text: str) -> tuple[str, bytes | None]:
    m = _BOARD_TAG.search(text)
    if not m: return text, None
    fen = m.group(1).strip()
    clean = _BOARD_TAG.sub('', text).strip()
    image = fen_to_image(fen)
    if image is None:
        board_text = fen_to_board(fen)
        return (clean + '\n\n' + board_text).strip() if board_text else clean, None
    return clean, image

def _db_chain(parent_id: int | None) -> list[dict]:
    if not parent_id: return []
    max_msgs = config.get("context", {}).get("max_messages", 40)
    return db.get_message_chain(parent_id, limit=max_msgs)

# ── Core helpers ─────────────────────────────────────────────────────────────

async def stream_to_placeholder(placeholder: discord.Message, gen) -> tuple[str, dict | None]:
    """Consume the llm.complete() async generator, editing placeholder with hybrid throttle.

    Hybrid throttle: edit when 0.3s elapsed OR 50 chars accumulated, whichever first.
    Live display strips <think> blocks but does NOT substitute @Name → <@ID>.
    120-second wall-clock timeout: appends "[generation timed out]" if exceeded.
    If completion_tokens >= 1000: appends "[token limit reached]".
    Returns (full_text, usage_meta).
    """
    full_text = ""
    usage_meta = None
    last_edit = 0.0
    buffer_since_last_edit = ""
    start_time = time.time()
    timed_out = False

    try:
        async for chunk, meta in gen:
            if chunk:
                full_text += chunk
                buffer_since_last_edit += chunk
                now = time.time()

                if now - start_time > 120:
                    timed_out = True
                    break

                should_edit = (now - last_edit >= 0.3) or (len(buffer_since_last_edit) >= 50)
                if should_edit:
                    display = re.sub(r"<think>.*?</think>", "", full_text, flags=re.DOTALL)
                    display = re.sub(r"<think>.*", "", display, flags=re.DOTALL).strip()
                    if display:
                        try:
                            await placeholder.edit(content=display[:1990])
                        except Exception:
                            pass
                    last_edit = now
                    buffer_since_last_edit = ""
            if meta:
                usage_meta = meta
    finally:
        await gen.aclose()

    if timed_out:
        full_text += "\n\n-# *[generation timed out]*"
    elif usage_meta and usage_meta.get("completion_tokens", 0) >= 1000:
        full_text += "\n\n-# *[token limit reached]*"

    return full_text, usage_meta


def resolve_inline_mentions(cleaned: str, mentions_map: dict) -> tuple[str, list[str]]:
    """Single left-to-right scan replacing @Name patterns and collecting <@ID> tags.

    Matches both @Name (from mentions_map keys) and bare <@ID> tags the LLM wrote.
    All matches merged by character offset (first occurrence position).
    Returns (substituted_text, found_mentions) where found_mentions is ordered by
    first character position in the original text.
    """
    # Build a list of (start_pos, end_pos, tag) for all matches
    matches: list[tuple[int, int, str]] = []

    # Match @Name patterns from the mentions_map
    for name, data in mentions_map.items():
        tag = data["tag"]
        pattern = re.compile(rf"@\b{re.escape(name)}\b", re.IGNORECASE)
        for m in pattern.finditer(cleaned):
            matches.append((m.start(), m.end(), tag))

    # Match bare <@ID> tags the LLM wrote directly
    bare_tag_re = re.compile(r"<@!?\d+>")
    for m in bare_tag_re.finditer(cleaned):
        matches.append((m.start(), m.end(), m.group(0)))

    if not matches:
        return cleaned, []

    # Sort by start position, then apply substitutions right-to-left to preserve offsets
    matches.sort(key=lambda x: x[0])

    # Deduplicate: track which tags we've already seen, ordered by first occurrence
    seen: dict[str, bool] = {}
    found_mentions: list[str] = []
    for _, _, tag in matches:
        # Normalise tag (strip ! from <@!ID>)
        normalised = re.sub(r"<@!", "<@", tag)
        if normalised not in seen:
            seen[normalised] = True
            found_mentions.append(normalised)

    # Apply substitutions right-to-left (highest offset first) to keep positions valid
    result = cleaned
    for start, end, tag in sorted(matches, key=lambda x: x[0], reverse=True):
        # Only replace @Name patterns (bare <@ID> tags stay as-is)
        if result[start] == "@" and result[start:end] != tag:
            result = result[:start] + tag + result[end:]

    return result, found_mentions


async def resolve_reply_target(
    found_mentions: list[str],
    mentions_map: dict,
    channel,
    guild,
) -> discord.Message | None:
    """Resolve the first mentioned user to their most recent message.

    Priority:
    1. Thread history via mentions_map last_msg_id → channel.fetch_message()
    2. channel.history(limit=100) scan for that user ID
    3. None

    Uses ONLY the first element of found_mentions to avoid ambiguous redirects.
    """
    if not found_mentions:
        return None

    first_tag = found_mentions[0]
    # Normalise tag — strip ! variant
    first_tag = re.sub(r"<@!", "<@", first_tag)
    # Extract numeric user ID
    id_match = re.search(r"<@(\d+)>", first_tag)
    if not id_match:
        return None
    user_id = int(id_match.group(1))

    # Priority 1: look up last_msg_id in mentions_map
    for data in mentions_map.values():
        tag = re.sub(r"<@!", "<@", data.get("tag", ""))
        if tag == first_tag and data.get("last_msg_id"):
            try:
                return await channel.fetch_message(data["last_msg_id"])
            except Exception:
                continue  # Try next matching entry before falling back to history scan

    # Priority 2: scan channel history
    try:
        async for msg in channel.history(limit=100):
            if msg.author.id == user_id:
                return msg
    except Exception:
        pass

    return None


def build_response(
    cleaned: str,
    style: dict,
    thinking: str,
    usage_meta: dict | None,
    found_mentions: list[str],
) -> tuple[str, discord.Embed]:
    """Build the content string and embed for the final Discord message.

    content: space-joined pings from found_mentions + thinking spoiler.
    embed: make_embed with footer "{persona_footer} | {model_name} | {N} tok | {tps:.1f} t/s".
    Always returns an embed (no plain-text path).
    """
    thinking_display = format_thinking_spoiler(thinking)

    # Content field: pings (always if present) + thinking spoiler
    parts = []
    if found_mentions:
        parts.append(" ".join(found_mentions))
    if thinking_display:
        parts.append(thinking_display)
    content = "\n".join(parts) if parts else None

    # Build embed
    embed = make_embed(cleaned[:EMBED_DESC_LIMIT], style)

    # Footer
    persona_footer = style.get("footer", "").strip() if style else ""
    if usage_meta:
        tps = usage_meta["completion_tokens"] / usage_meta["duration"] if usage_meta.get("duration", 0) > 0 else 0
        model_name = usage_meta["model"].split("/")[-1]
        tok_str = f"{usage_meta['completion_tokens']} tok"
        footer_parts = [p for p in [persona_footer, model_name, tok_str, f"{tps:.1f} t/s"] if p]
        embed.set_footer(text=" | ".join(footer_parts))
    elif persona_footer:
        embed.set_footer(text=persona_footer)

    return content, embed


async def send_final(
    placeholder: discord.Message,
    reply_to: discord.Message | None,
    reply_target: discord.Message | None,
    content: str | None,
    embed: discord.Embed,
    view,
    channel,
) -> discord.Message:
    """Send or edit the final response, redirecting to reply_target if different from reply_to.

    Case 1: reply_target is None OR same message as reply_to → edit placeholder in-place.
    Case 2: reply_target is a different message → delete placeholder, reply to reply_target.
            Falls back to channel.send() if reply raises.
    Returns the discord.Message that was actually sent/edited.
    """
    redirect = (
        reply_target is not None
        and (reply_to is None or reply_target.id != reply_to.id)
    )

    if not redirect:
        await placeholder.edit(content=content, embed=embed, view=view)
        return placeholder

    # Delete placeholder and send to the new target
    try:
        await placeholder.delete()
    except Exception:
        pass

    try:
        return await reply_target.reply(content=content, embed=embed, view=view)
    except Exception:
        return await channel.send(content=content, embed=embed, view=view)


async def send_webhook(channel, messages, persona, parent_msg_id, temperature, provider, model):
    """Generate fully (no streaming) and send via webhook for sim-city channel.

    Handles its own db.save_message() and db.log_usage() using the real sent_msg.id.
    """
    webhook = await bot.get_or_create_webhook(channel)
    if not webhook:
        log.error("send_webhook: could not obtain webhook for %s", channel)
        return

    full_text = ""
    usage_meta = None
    gen = llm.complete(messages, provider, model, config, temperature=temperature, max_tokens=1000)
    async for chunk, meta in gen:
        if chunk:
            full_text += chunk
        if meta:
            usage_meta = meta

    thinking, raw_rest = extract_thinking(full_text)
    cleaned, _ = extract_board(raw_rest)

    thinking_display = format_thinking_spoiler(thinking)
    footer_extra = ""
    if usage_meta:
        tps = usage_meta["completion_tokens"] / usage_meta["duration"] if usage_meta.get("duration", 0) > 0 else 0
        model_name = usage_meta["model"].split("/")[-1]
        footer_extra = f" | {model_name} | {usage_meta['completion_tokens']} tok | {tps:.1f} t/s"

    meta_info = get_persona_metadata(persona)
    display_name = meta_info.get("display_name", persona)
    avatar_url = meta_info.get("avatar_url")

    content = (thinking_display + "\n\n" + cleaned) if thinking_display else cleaned
    if footer_extra:
        content += f"\n\n-# *{footer_extra.strip(' |')}*"

    sent_msg = await webhook.send(
        content=content[:1990],
        username=display_name,
        avatar_url=avatar_url,
        wait=True,
    )

    db.save_message(sent_msg.id, parent_msg_id, channel.id, "assistant", cleaned)
    if usage_meta:
        db.log_usage(sent_msg.id, model, provider,
                     usage_meta["prompt_tokens"], usage_meta["completion_tokens"], usage_meta["duration"])


# ── Orchestrator ─────────────────────────────────────────────────────────────

async def process_llm_request(channel, messages, persona, parent_msg_id, reply_to=None, temperature=None, mentions_map=None):
    provider = bot.current_provider
    lock = get_llm_lock(provider)

    async with (lock if lock else contextlib.AsyncExitStack()):
        is_sim_city = getattr(channel, 'name', None) == "sim-city"
        if is_sim_city:
            await send_webhook(channel, messages, persona, parent_msg_id, temperature, provider, bot.current_model)
            return

        gen = llm.complete(messages, provider, bot.current_model, config, temperature=temperature, max_tokens=1000)
        placeholder = await (reply_to.reply if reply_to else channel.send)(_THINKING_SIGNAL)

        try:
            full_text, meta = await stream_to_placeholder(placeholder, gen)
        except Exception as e:
            log.error("LLM Error: %s", e)
            await placeholder.edit(content=f"⚠️ Error: {e}")
            return

        thinking, cleaned = extract_thinking(full_text)
        cleaned, board_image = extract_board(cleaned)
        cleaned, found_mentions = resolve_inline_mentions(cleaned, mentions_map or {})

        guild = getattr(channel, 'guild', None)
        reply_target = await resolve_reply_target(found_mentions, mentions_map or {}, channel, guild)

        style = get_style(persona, load_persona_style(persona))
        view = ResponseView(bot_callback=bot.handle_view_interaction)
        content, embed = build_response(cleaned, style, thinking, meta, found_mentions)

        sent_msg = await send_final(placeholder, reply_to, reply_target, content, embed, view, channel)
        db.save_message(sent_msg.id, parent_msg_id, channel.id, "assistant", cleaned)
        if meta:
            db.log_usage(sent_msg.id, bot.current_model, provider,
                         meta["prompt_tokens"], meta["completion_tokens"], meta["duration"])

        if board_image:
            await channel.send(file=discord.File(io.BytesIO(board_image), filename="board.png"), reference=sent_msg)


async def handle_summarize(channel_id: int) -> str:
    provider = bot.current_provider
    lock = get_llm_lock(provider)
    async def _run():
        with db._conn:
            rows = db._conn.execute("SELECT role, content FROM messages WHERE channel_id = ? ORDER BY discord_msg_id DESC LIMIT 20", (channel_id,)).fetchall()
        text = "\n".join(f"{r['role']}: {r['content']}" for r in reversed(rows))
        return await llm.summarize(text, provider, bot.current_model, config)

    if lock:
        async with lock: return await _run()
    return await _run()

# ── Slash Commands ───────────────────────────────────────────────────────────

@bot.tree.command(name="help", description="Show the bot's command guide")
async def help_cmd(interaction: discord.Interaction):
    try:
        with open("CHEATSHEET.md", "r", encoding="utf-8") as f:
            content = f.read()
        embed = discord.Embed(title="📖 bot guide", description=content, color=0x2B2D31)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"Could not load guide: {e}", ephemeral=True)

@bot.tree.command(name="options", description="Open settings for this channel")
async def options(interaction: discord.Interaction):
    view = await _get_options_view(interaction.channel_id, interaction.client)
    await interaction.response.send_message(embed=_options_embed(interaction.channel_id, interaction.client), view=view, ephemeral=True)

@bot.tree.command(name="persona", description="Switch the active persona")
@app_commands.describe(name="The name of the persona")
async def persona(interaction: discord.Interaction, name: str):
    if load_persona(name) is None:
        await interaction.response.send_message(f"Persona `{name}` not found.", ephemeral=True)
        return
    db.set_channel_persona(interaction.channel_id, name)
    db.clear_channel(interaction.channel_id)
    await interaction.response.send_message(f"· now speaking as: **{name}** ·", ephemeral=True)

@persona.autocomplete("name")
async def persona_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=p, value=p) for p in list_personas() if current.lower() in p.lower()][:25]

@bot.tree.command(name="reset", description="Clear conversation history")
async def reset(interaction: discord.Interaction):
    db.clear_channel(interaction.channel_id)
    await interaction.response.send_message("·˚ slate wiped ˚·", ephemeral=True)

@bot.tree.command(name="context", description="Show the current context being sent to the LLM")
async def show_context(interaction: discord.Interaction):
    with db._conn:
        rows = db._conn.execute("SELECT role, content FROM messages WHERE channel_id = ? ORDER BY discord_msg_id DESC LIMIT 10", (interaction.channel_id,)).fetchall()
    if not rows:
        await interaction.response.send_message("Context is empty.", ephemeral=True)
        return
    body = "\n".join(f"**{r['role']}**: {r['content'][:100]}..." for r in reversed(rows))
    await interaction.response.send_message(f"**Recent Context:**\n{body}", ephemeral=True)

@bot.tree.command(name="personas", description="List all available personas")
async def personas_cmd(interaction: discord.Interaction):
    names = list_personas()
    active = db.get_channel_persona(interaction.channel_id) or config.get("persona", "mochi")
    lines = [f"→ **{n}** *(active)*" if n == active else f"  {n}" for n in names]
    await interaction.response.send_message("· ★ · **personas** · ★ ·\n```\n" + "\n".join(lines) + "\n```", ephemeral=True)

@bot.tree.command(name="prompt", description="Show the current persona's system prompt")
async def prompt_cmd(interaction: discord.Interaction):
    active = db.get_channel_persona(interaction.channel_id) or config.get("persona", "mochi")
    text = load_persona(active) or f"(no persona file for {active})"
    await interaction.response.send_message(f"**Active persona:** `{active}`\n\n{text}"[:2000], ephemeral=True)

@bot.tree.command(name="verbosity", description="Set the response length (1-5)")
async def verbosity_cmd(interaction: discord.Interaction, level: int):
    if 1 <= level <= 5:
        db.set_channel_verbosity(interaction.channel_id, level)
        await interaction.response.send_message(f"· verbosity **{level}/5** · {VERBOSITY_LABELS.get(level)}", ephemeral=True)
    else:
        await interaction.response.send_message("· level must be between 1 and 5 ·", ephemeral=True)

@bot.tree.command(name="temperature", description="Set model temperature (0.0 - 2.0)")
async def temperature_cmd(interaction: discord.Interaction, value: float):
    if 0.0 <= value <= 2.0:
        db.set_channel_temperature(interaction.channel_id, value)
        await interaction.response.send_message(f"· temperature set to **{value}** ·", ephemeral=True)
    else:
        await interaction.response.send_message("· temperature must be between 0.0 and 2.0 ·", ephemeral=True)

@bot.tree.command(name="level", description="Set Stockfish difficulty level (1-8)")
async def level_cmd(interaction: discord.Interaction, level: int | None = None):
    if level is None:
        depth, elo = chess_engine.CHESS_LEVEL_MAP[bot.chess_level]
        await interaction.response.send_message(f"· stockfish · **level {bot.chess_level}/8** · {elo} Elo · depth {depth} ·", ephemeral=True)
        return
    if 1 <= level <= 8:
        bot.chess_level = level
        depth, elo = chess_engine.CHESS_LEVEL_MAP[level]
        await interaction.response.send_message(f"· difficulty dialled to **level {level}/8** · {elo} Elo · depth {depth} ·", ephemeral=True)
    else:
        await interaction.response.send_message("· level must be between 1 and 8 ·", ephemeral=True)

@bot.command(name="provider")
@commands.is_owner()
async def provider_prefix(ctx: commands.Context, name: str):
    """Legacy prefix command for switching providers."""
    name = name.lower()
    if name not in config.get("providers", {}):
        await ctx.send(f"Unknown provider `{name}`.")
        return
    bot.current_provider = name
    config["default_provider"] = name
    if name == "local":
        models = await llm.get_local_models(config)
        bot.current_model = models[0] if models else "local-model"
    else:
        models = config["providers"][name].get("models", [])
        bot.current_model = models[0] if models else "unknown"
    config["default_model"] = bot.current_model
    save_config(config)
    await ctx.send(f"Switched to **{name}** provider, model **{bot.current_model}**.")

@bot.tree.command(name="provider", description="Switch LLM provider")
async def provider_cmd(interaction: discord.Interaction, name: str):
    name = name.lower()
    if name not in config.get("providers", {}):
        await interaction.response.send_message(f"Unknown provider `{name}`.", ephemeral=True)
        return
    interaction.client.current_provider = name
    config["default_provider"] = name
    if name == "local":
        models = await llm.get_local_models(config)
        interaction.client.current_model = models[0] if models else "local-model"
    else:
        models = config["providers"][name].get("models", [])
        interaction.client.current_model = models[0] if models else "unknown"
    config["default_model"] = interaction.client.current_model
    save_config(config)
    await interaction.response.send_message(f"Switched to **{name}** provider, model **{interaction.client.current_model}**.", ephemeral=True)

@provider_cmd.autocomplete("name")
async def provider_autocomplete(interaction: discord.Interaction, current: str):
    providers = list(config.get("providers", {}).keys())
    return [app_commands.Choice(name=p, value=p) for p in providers if current.lower() in p.lower()]

@bot.tree.command(name="restart", description="Reboot the bot process")
@commands.is_owner()
async def restart_cmd(interaction: discord.Interaction):
    await interaction.response.send_message("·˚ rebooting · back in a moment ˚·", ephemeral=True)
    await asyncio.sleep(0.5)
    if _INSTANCE_LOCK: _INSTANCE_LOCK.close()
    subprocess.Popen([sys.executable] + sys.argv)
    await bot.close()

@bot.tree.command(name="model", description="Switch the current model")
@app_commands.describe(name="The model ID")
async def model_cmd(interaction: discord.Interaction, name: str):
    interaction.client.current_model = name
    config["default_model"] = name
    save_config(config)
    await interaction.response.send_message(f"Model switched to **{name}**.", ephemeral=True)

@model_cmd.autocomplete("name")
async def model_autocomplete(interaction: discord.Interaction, current: str):
    current = current.lower()
    provider = interaction.client.current_provider
    if provider == "local":
        models = await llm.get_local_models(config)
    elif provider == "openrouter":
        models = await llm.get_openrouter_models(config)
    else:
        models = config["providers"].get(provider, {}).get("models", [])
    choices = [app_commands.Choice(name=m, value=m) for m in models if current in m.lower()][:25]
    return choices

# ── Events ───────────────────────────────────────────────────────────────────

@bot.tree.command(name="resign", description="Resign the current chess game")
async def resign(interaction: discord.Interaction):
    persona = db.get_channel_persona(interaction.channel_id) or config.get("persona", "mochi")
    if not chess_engine.is_any_chess_persona(persona):
        await interaction.response.send_message("· no game in progress · no king to topple ·", ephemeral=True)
        return
    fen = chess_engine.current_fen(interaction.channel_id)
    chess_engine.reset_game(interaction.channel_id)
    text, board_image = extract_board(f"{random.choice(_RESIGN_MSGS)}\n\n[board: {fen}]")
    await interaction.response.send_message(text)
    if board_image:
        await interaction.followup.send(file=discord.File(io.BytesIO(board_image), filename="board.png"))

@bot.event
async def on_message(message: discord.Message):
    await bot.process_commands(message)
    if message.author.bot and not message.webhook_id: return

    is_sim_city = message.channel.name == "sim-city" if hasattr(message.channel, 'name') else False
    if message.webhook_id and not is_sim_city: return

    is_mentioned = bot.user in message.mentions
    is_dm = isinstance(message.channel, discord.DMChannel)

    target_persona = None
    prompt = message.content

    if is_sim_city:
        # Check for [PersonaName] pattern
        m = re.search(r"^\[(.*?)]", prompt)
        if m:
            potential_persona = m.group(1).strip()
            persona_list = list_personas()
            if potential_persona.lower() in [p.lower() for p in persona_list]:
                target_persona = next(p for p in persona_list if p.lower() == potential_persona.lower())
                prompt = prompt[m.end():].strip()

        # If no explicit tag, check if it's a reply to one of our previous messages
        if not target_persona and message.reference:
            ref = db.get_message(message.reference.message_id)
            if ref and ref['role'] == 'assistant':
                target_persona = ch_persona(message.channel.id)

        if not target_persona and (is_mentioned or is_dm):
            target_persona = ch_persona(message.channel.id)
    else:
        if message.author.bot: return
        if not (is_dm or is_mentioned):
            return
        target_persona = ch_persona(message.channel.id)

    if not target_persona:
        return

    prompt = re.sub(rf"<@!?{bot.user.id}>", "", prompt).strip()
    if not prompt and not message.attachments:
        if is_mentioned:
            await message.reply("You mentioned me but didn't say anything!")
        return

    persona = target_persona
    is_chess = chess_engine.is_chess_persona(persona)
    is_chess_classic = chess_engine.is_chess_classic_persona(persona)

    if (is_chess or is_chess_classic) and prompt:
        ok, san_or_err, _ = chess_engine.apply_user_move(message.channel.id, prompt)
        if not ok:
            await message.reply(san_or_err)
            return

    if is_chess_classic:
        async with message.channel.typing():
            depth = 12
            result = await chess_api.get_stockfish_move(chess_engine.current_fen(message.channel.id), depth=depth)
            if not result:
                await message.reply("· the oracle is silent · API unreachable ·")
                return
            chess_engine.apply_bot_move(message.channel.id, result["move"])
            status = chess_engine.game_status(message.channel.id)
            reply_text = f"**{result['san']}**"
            if status: reply_text += f"\n\n{_chess_result_text(status)}"
            reply_text += f"\n\n[board: {chess_engine.current_fen(message.channel.id)}]"
            clean, board_image = extract_board(reply_text)
            await message.reply(clean)
            if board_image:
                await message.channel.send(file=discord.File(io.BytesIO(board_image), filename="board.png"))
        return

    parent_id = message.reference.message_id if message.reference else None
    chain = _db_chain(parent_id)

    # Resolve mentions from the history chain and the current message
    mentions_map = resolve_mentions(chain, message.guild)
    for m in message.mentions:
        if m.id != bot.user.id:
            data = {"tag": f"<@{m.id}>", "last_msg_id": message.id}
            mentions_map[m.display_name] = data
            mentions_map[m.name] = data
            mentions_map[str(m.id)] = data

    system = get_system_prompt(persona, message.channel.id, mentions_map=mentions_map)
    if is_chess:
        fen_now = chess_engine.current_fen(message.channel.id)
        status = chess_engine.game_status(message.channel.id)
        system += (
            f"\n\n## Chess Board State\nFEN: `{fen_now}`\n"
            f"Legal moves: {chess_engine.legal_moves_str(message.channel.id)}"
        )
        if status: system += f"\n**Game over: {status}**"

    db.save_message(message.id, parent_id, message.channel.id, "user", prompt, author_id=message.author.id)
    image_blocks = await llm.format_image_blocks(message.attachments)
    user_content = ([{"type": "text", "text": prompt or " "}] + image_blocks) if image_blocks else (prompt or " ")
    messages_payload = [{"role": "system", "content": system}] + chain + [{"role": "user", "content": user_content}]
    temp = db.get_channel_temperature(message.channel.id)

    if is_sim_city:
        await process_llm_request(message.channel, messages_payload, persona, message.id,
                                  reply_to=message, temperature=temp, mentions_map=mentions_map)
    else:
        async with message.channel.typing():
            await process_llm_request(message.channel, messages_payload, persona, message.id,
                                      reply_to=message, temperature=temp, mentions_map=mentions_map)

@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)
    # Generate avatars for all personas in the background
    async def _bg_gen():
        for p in list_personas():
            try:
                await asyncio.to_thread(generate_avatar, p)
            except Exception as e:
                log.error(f"Failed to generate avatar for {p}: {e}")
    asyncio.create_task(_bg_gen())

if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN")
    if not token: raise RuntimeError("DISCORD_TOKEN not set")
    try:
        _INSTANCE_LOCK = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        _INSTANCE_LOCK.bind(("127.0.0.1", SINGLETON_PORT))
    except OSError:
        log.warning("Another instance is already running. Exiting.")
        sys.exit(0)
    bot.run(token)
