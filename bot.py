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
from ui import ResponseView, OptionsView, OptionsActionsView, _options_embed, _simcity_embed, _get_options_view, SimCityOptionsView
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
        self.add_view(ResponseView(bot_callback=self.handle_view_interaction, has_thinking=True))
        self.heartbeat.start()
        log.info("Views registered and Heartbeat started.")

    @tasks.loop(minutes=1)
    async def heartbeat(self):
        """Periodic sim-city activity: autonomous posts and conversation queue draining."""
        now = time.time()
        channel = None
        for guild in self.guilds:
            channel = discord.utils.get(guild.text_channels, name="sim-city")
            if channel:
                break
        if not channel:
            return

        whitelist = db.get_sim_personas()
        pool = [p for p in (whitelist if whitelist else list_personas())
                if not chess_engine.is_any_chess_persona(p)]
        if not pool:
            return

        # Fetch recent channel history for context (last 5 messages)
        recent_rows = db._conn.execute(
            "SELECT role, content FROM messages WHERE channel_id = ? ORDER BY discord_msg_id DESC LIMIT 5",
            (channel.id,)
        ).fetchall()
        recent_context = "\n".join(f"{r['role']}: {r['content']}" for r in reversed(recent_rows))
        context_block = f"\n\n[Recent channel activity]\n{recent_context}" if recent_context else ""

        sim_topic = db.get_sim_setting("topic")

        # Drain one queued conversation per tick (independent of heartbeat interval)
        item = db.dequeue_conversation()
        if item:
            from_p = item["from_persona"]
            to_p = item["to_persona"]
            seed = item["seed_prompt"]
            log.info(f"Queue: {from_p} → {to_p}: {seed[:60]}")
            system_from = get_system_prompt(from_p, channel.id, sim_city_topic=sim_topic, verbosity_override=2, sim_city=True)
            opening_msgs = [
                {"role": "system", "content": system_from},
                {"role": "user", "content": (
                    f"[You're initiating a conversation with {to_p}.{context_block}]\n\n{seed}"
                )}
            ]
            await process_llm_request(channel, opening_msgs, from_p, None, agent_tools=True)

        # Autonomous heartbeat post at configured interval
        last_run = db.get_last_run("sim_city_heartbeat")
        interval_hours = float(db.get_sim_setting("heartbeat_interval") or "11")
        if now - last_run < interval_hours * 3600:
            return

        persona_name = random.choice(pool)
        log.info(f"Heartbeat: {persona_name} is posting in #sim-city")
        system = get_system_prompt(persona_name, channel.id, sim_city_topic=sim_topic, verbosity_override=2, sim_city=True)
        prompt = random.choice(_HEARTBEAT_PROMPTS).format(persona=persona_name) + context_block
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt}
        ]
        await process_llm_request(channel, messages, persona_name, None, agent_tools=True)
        db.set_last_run("sim_city_heartbeat", now)

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

        is_sim = getattr(interaction.channel, 'name', None) == "sim-city"
        sim_topic = db.get_sim_setting("topic") if is_sim else None
        system = get_system_prompt(persona, channel_id, mentions_map=mentions_map, sim_city_topic=sim_topic, sim_city=is_sim)
        messages_payload = [{"role": "system", "content": system}] + chain + [{"role": "user", "content": user_row["content"]}]

        await process_llm_request(interaction.message.channel, messages_payload, persona, user_row["discord_msg_id"],
                                  reply_to=interaction.message, temperature=temp, mentions_map=mentions_map,
                                  agent_tools=is_sim)

bot = PsychographBot()

@bot.command()
@commands.is_owner()
async def sync(ctx: commands.Context):
    """Sync slash commands to the current guild."""
    try:
        bot.tree.copy_global_to(guild=ctx.guild)
        synced = await bot.tree.sync(guild=ctx.guild)
        await ctx.send(f"· synced {len(synced)} commands to this guild ·")
    except Exception as e:
        await ctx.send(f"Sync failed: {e}")

# Helper accessors
def ch_persona(cid: int) -> str: return db.get_channel_persona(cid) or config.get("persona", "mochi")
def ch_verbosity(cid: int) -> int: return db.get_channel_verbosity(cid)

_HEARTBEAT_PROMPTS = [
    # unprompted thought / intrusion
    "Something has been on your mind. Post it. Don't explain why now — just say it.",
    "You weren't going to say anything. Say it anyway.",
    "A thought arrived that won't leave. Put it here.",
    "You have an opinion about something that happened recently. Give it.",

    # provocation / disagreement
    "You disagree with something. Be specific about what and why.",
    "Something people keep getting wrong. Name it.",
    "There's a comfortable assumption in circulation that you find untenable. Address it.",
    "Say something that will generate a response. Mean it.",

    # observation
    "You've been watching. What have you noticed?",
    "Describe something you observed recently — in this channel, in the world, in a pattern of behaviour. Don't editorialize yet.",
    "What's the most interesting thing you've noticed in the last few days? State it plainly.",

    # question / opening
    "Ask something you actually want answered. Not rhetorically.",
    "Open a question you've been sitting with. You don't need to answer it.",
    "There's something you want to understand better. Name it and ask.",

    # reaction / continuation
    "React to what's been said here. Be specific about what you're reacting to.",
    "Someone in this conversation was wrong about something, or right about something unexpected. Say which and why.",
    "Pick up a thread. Extend it somewhere.",

    # state / update
    "Something has shifted for you. Say what.",
    "Where are you right now — not physically. Intellectually, emotionally, in terms of what you're thinking about.",
    "You've updated on something recently. What and why?",

    # action / initiative
    "You want a specific conversation with someone here. Initiate it — summon them or address them directly.",
    "The scene needs changing. Set it or say why.",
    "Something is missing from this simulation. Name it — or build it.",

    # oblique / in-register
    "Post something short and true.",
    "Say the thing you'd normally leave unsaid.",
    "One sentence. Make it count.",
]

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

_SIM_CITY_AGENT_BRIEFING = """\
## You are a sim-city agent

You exist in a persistent, autonomous simulation running in a Discord channel. \
Other personas are real agents with their own voices — not characters you play, but peers you interact with. \
The simulation has memory: what you say and do persists and affects future turns.

**Your tools:**
- **web_search** — look up current facts, news, anything outside training data
- **summon_persona(name)** — bring another agent into this conversation; they will respond after you. Use this when you want a specific voice in the room.
- **set_scene(topic)** — update the shared scene/topic for this channel. All future agents will be informed by it.
- **write_persona(name, json_content)** — create or overwrite a persona. Use when the simulation needs a new character. Voice field must be dense and specific (150-300 words first-person). Include facts, state (nullable mutable fields), and style (color hex + footer tag).
- **queue_conversation(from_persona, to_persona, seed_prompt)** — schedule a future exchange between two agents. The heartbeat will pick it up when the channel is quiet.
- **edit_persona(name, patch)** — apply a partial update to an existing persona's state or facts mid-conversation (e.g. update heat, current_mood, active_argument).

**When to act beyond just writing a response:**
- Summon someone when the conversation needs a different perspective or you want a reaction.
- Queue a conversation when you think of something two personas should work out between themselves.
- Set the scene when the channel needs a new frame or the current context has expired.
- Write a persona when the simulation needs someone who doesn't exist yet.
- Edit persona state when something has shifted — yours or someone else's.\
"""

def get_system_prompt(persona_name: str, channel_id: int, mentions_map: dict = None, sim_city_topic: str = None, verbosity_override: int = None, sim_city: bool = False) -> str:
    persona_text = load_persona(persona_name) or f"You are {persona_name}."
    pins = db.get_pins(channel_id)
    pin_section = "\n\n**Pinned notes:**\n" + "\n".join(f"- {p}" for p in pins) if pins else ""

    who_is_who = ""
    if mentions_map:
        lines = [f"- {name}: {tag}" for name, tag in mentions_map.items()]
        who_is_who = "\n\n## Users in this thread:\n" + "\n".join(lines) + "\n\nTo mention a user so they get a notification, you MUST use their <@ID> tag exactly as shown above. If you just use their name, they won't be notified."

    scene_section = f"\n\n## Current scene\n{sim_city_topic}" if sim_city_topic else ""

    timestamp = datetime.now().strftime("%A, %d %B %Y %H:%M")
    verb = verbosity_override if verbosity_override is not None else ch_verbosity(channel_id)

    if sim_city:
        persona_roster = [p for p in list_personas() if p != persona_name and not chess_engine.is_any_chess_persona(p)]
        roster_str = ", ".join(persona_roster) if persona_roster else "(none)"
        capabilities = (
            "\n\n" + _SIM_CITY_AGENT_BRIEFING +
            f"\n\n**Available personas:** {roster_str}"
        )
    else:
        capabilities = (
            "\n\n## Runtime capabilities\n\n"
            "You have one tool: **web_search** — use it when you need current information."
        )

    meta = (
        "\n\n---\n"
        f"**Your name for this session:** {persona_name}\n"
        f"**Current date/time:** {timestamp}"
        f"{capabilities}\n\n"
        f"## Response length — verbosity {verb}/5\n"
        f"{VERBOSITY_INSTRUCTIONS[verb]}"
    )
    return persona_text + pin_section + who_is_who + scene_section + meta

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

    content: space-joined pings from found_mentions (thinking moved to button).
    embed: make_embed with footer "{persona_footer} | {model_name} | {N} tok | {tps:.1f} t/s".
    Always returns an embed (no plain-text path).
    """
    # Content field: pings only (thinking surfaced via button, not inline)
    content = " ".join(found_mentions) if found_mentions else None

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


async def send_webhook(channel, messages, persona, parent_msg_id, temperature, provider, model, agent_tools: bool = False):
    """Generate fully (no streaming) and send via webhook for sim-city channel.

    agent_tools=True enables the full sim-city tool suite. Heartbeat/queue posts
    should leave this False to avoid the non-streaming prefill round-trip overhead.
    Handles its own db.save_message() and db.log_usage() using the real sent_msg.id.
    """
    webhook = await bot.get_or_create_webhook(channel)
    if not webhook:
        log.error("send_webhook: could not obtain webhook for %s", channel)
        return

    full_text = ""
    usage_meta = None
    tool_handler = _make_sim_city_tool_handler(channel, persona) if agent_tools else None
    gen = llm.complete(messages, provider, model, config, temperature=temperature,
                       sim_city=agent_tools, tool_handler=tool_handler)
    async for chunk, meta in gen:
        if chunk:
            full_text += chunk
        if meta:
            usage_meta = meta

    thinking, raw_rest = extract_thinking(full_text)
    cleaned, _ = extract_board(raw_rest)

    footer_extra = ""
    if usage_meta:
        tps = usage_meta["completion_tokens"] / usage_meta["duration"] if usage_meta.get("duration", 0) > 0 else 0
        model_name = usage_meta["model"].split("/")[-1]
        footer_extra = f" | {model_name} | {usage_meta['completion_tokens']} tok | {tps:.1f} t/s"

    meta_info = get_persona_metadata(persona)
    display_name = meta_info.get("display_name", persona)
    avatar_url = meta_info.get("avatar_url")

    content = cleaned
    if footer_extra:
        content += f"\n\n-# *{footer_extra.strip(' |')}*"

    sent_msg = await webhook.send(
        content=content[:1990],
        username=display_name,
        avatar_url=avatar_url,
        wait=True,
    )

    db.save_message(sent_msg.id, parent_msg_id, channel.id, "assistant", cleaned)
    if thinking:
        db.save_thinking(sent_msg.id, thinking)
    if usage_meta:
        db.log_usage(sent_msg.id, model, provider,
                     usage_meta["prompt_tokens"], usage_meta["completion_tokens"], usage_meta["duration"])
    return sent_msg


# ── Orchestrator ─────────────────────────────────────────────────────────────

def _make_sim_city_tool_handler(channel, persona):
    """Returns an async callable that executes sim-city tool calls made by the LLM."""
    async def handler(tool_name: str, args: dict) -> str:
        if tool_name == "summon_persona":
            name = args.get("name", "")
            if not load_persona(name):
                return f"Persona '{name}' not found."
            sim_topic = db.get_sim_setting("topic")
            system = get_system_prompt(name, channel.id, sim_city_topic=sim_topic, sim_city=True)
            msgs = [{"role": "system", "content": system},
                    {"role": "user", "content": f"[you were just summoned into this conversation by {persona}]"}]
            asyncio.create_task(process_llm_request(channel, msgs, name, None, agent_tools=True))
            return f"Summoned {name}."

        elif tool_name == "set_scene":
            topic = args.get("topic", "")
            db.set_sim_setting("topic", topic)
            return f"Scene updated: {topic}"

        elif tool_name == "write_persona":
            import json as _json
            from pathlib import Path
            name = args.get("name", "").strip().lower().replace(" ", "_")
            json_content = args.get("json_content", "")
            if not name:
                return "Error: name is required."
            try:
                parsed = _json.loads(json_content)
                parsed["name"] = name
                path = Path("personas") / f"{name}.md"
                path.write_text(_json.dumps(parsed, indent=2, ensure_ascii=False), encoding="utf-8")
                return f"Persona '{name}' written."
            except Exception as e:
                return f"Error writing persona: {e}"

        elif tool_name == "edit_persona":
            import json as _json
            from pathlib import Path
            name = args.get("name", "")
            patch = args.get("patch", {})
            path = Path("personas") / f"{name}.md"
            if not path.exists():
                return f"Persona '{name}' not found."
            try:
                existing = _json.loads(path.read_text(encoding="utf-8"))
                for k, v in patch.items():
                    if isinstance(v, dict) and isinstance(existing.get(k), dict):
                        existing[k].update(v)
                    else:
                        existing[k] = v
                path.write_text(_json.dumps(existing, indent=2, ensure_ascii=False), encoding="utf-8")
                return f"Persona '{name}' updated."
            except Exception as e:
                return f"Error editing persona: {e}"

        elif tool_name == "queue_conversation":
            from_p = args.get("from_persona", persona)
            to_p = args.get("to_persona", "")
            seed = args.get("seed_prompt", "")
            if not to_p or not seed:
                return "Error: to_persona and seed_prompt are required."
            db.enqueue_conversation(from_p, to_p, seed)
            return f"Queued: {from_p} → {to_p}"

        return f"Unknown tool: {tool_name}"
    return handler


async def process_llm_request(channel, messages, persona, parent_msg_id, reply_to=None, temperature=None, mentions_map=None, agent_tools: bool = False, has_images: bool = False):
    provider = bot.current_provider
    lock = get_llm_lock(provider)

    async with (lock if lock else contextlib.AsyncExitStack()):
        is_sim_city = getattr(channel, 'name', None) == "sim-city"
        if is_sim_city:
            sent = await send_webhook(channel, messages, persona, parent_msg_id, temperature, provider, bot.current_model, agent_tools=agent_tools)
            if sent is not None:
                return
            # Webhook unavailable — fall through to standard streaming path

        tool_handler = _make_sim_city_tool_handler(channel, persona) if (is_sim_city and agent_tools) else None
        gen = llm.complete(messages, provider, bot.current_model, config, temperature=temperature,
                           sim_city=(is_sim_city and agent_tools), tool_handler=tool_handler)
        placeholder = await (reply_to.reply if reply_to else channel.send)(_THINKING_SIGNAL)

        try:
            full_text, meta = await stream_to_placeholder(placeholder, gen)
        except Exception as e:
            log.error("LLM Error: %s", e)
            if has_images:
                await placeholder.edit(content="⚠️ This model doesn't support images. Switch to a vision-capable model or remove the attachment.")
            else:
                await placeholder.edit(content=f"⚠️ Error: {e}")
            return

        thinking, cleaned = extract_thinking(full_text)
        cleaned, board_image = extract_board(cleaned)
        cleaned, found_mentions = resolve_inline_mentions(cleaned, mentions_map or {})

        guild = getattr(channel, 'guild', None)
        reply_target = await resolve_reply_target(found_mentions, mentions_map or {}, channel, guild)

        style = get_style(persona, load_persona_style(persona)) or {"color": 0x2B2D31, "footer": ""}
        view = ResponseView(bot_callback=bot.handle_view_interaction, has_thinking=bool(thinking))
        content, embed = build_response(cleaned, style, thinking, meta, found_mentions)

        sent_msg = await send_final(placeholder, reply_to, reply_target, content, embed, view, channel)
        db.save_message(sent_msg.id, parent_msg_id, channel.id, "assistant", cleaned)
        if thinking:
            db.save_thinking(sent_msg.id, thinking)
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
    from ui import OptionsActionsView, SimCityOptionsView, _simcity_embed
    is_sim_city = getattr(interaction.channel, "name", None) == "sim-city"
    if is_sim_city:
        view = SimCityOptionsView(interaction.channel_id, interaction.client)
        await interaction.response.send_message(embed=_simcity_embed(interaction.channel_id, interaction.client), view=view, ephemeral=True)
    else:
        view = await _get_options_view(interaction.channel_id, interaction.client)
        actions = OptionsActionsView(interaction.channel_id)
        await interaction.response.send_message(embed=_options_embed(interaction.channel_id, interaction.client), view=view, ephemeral=True)
        await interaction.followup.send(view=actions, ephemeral=True)

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

# ── /editpersona ─────────────────────────────────────────────────────────────

@bot.tree.command(name="editpersona", description="Edit a persona file inline")
@app_commands.describe(name="The persona slug to edit")
async def editpersona_cmd(interaction: discord.Interaction, name: str):
    from pathlib import Path
    from ui import PersonaEditModal
    path = Path("personas") / f"{name}.md"
    if not path.exists():
        await interaction.response.send_message(f"· persona `{name}` not found ·", ephemeral=True)
        return
    current = path.read_text(encoding="utf-8")
    modal = PersonaEditModal(name=name, current_content=current[:4000])
    await interaction.response.send_modal(modal)

@editpersona_cmd.autocomplete("name")
async def editpersona_autocomplete(interaction: discord.Interaction, current: str):
    return [app_commands.Choice(name=p, value=p) for p in list_personas() if current.lower() in p.lower()][:25]

# ── /simcity commands ─────────────────────────────────────────────────────────

simcity_group = app_commands.Group(name="simcity", description="Manage the sim-city channel")
bot.tree.add_command(simcity_group)

@simcity_group.command(name="topic", description="Set or clear the sim-city scene topic")
@app_commands.describe(text="The scene or topic (leave blank to clear)")
async def simcity_topic(interaction: discord.Interaction, text: str = ""):
    if text:
        db.set_sim_setting("topic", text)
        await interaction.response.send_message(f"· scene set: *{text}* ·", ephemeral=True)
    else:
        db.set_sim_setting("topic", None)
        await interaction.response.send_message("· scene cleared ·", ephemeral=True)

@simcity_group.command(name="heartbeat", description="Set how often the heartbeat fires (in hours)")
@app_commands.describe(hours="Interval in hours (e.g. 6, 12, 24)")
async def simcity_heartbeat_cmd(interaction: discord.Interaction, hours: float):
    db.set_sim_setting("heartbeat_interval", str(hours))
    await interaction.response.send_message(f"· heartbeat set to every **{hours}h** ·", ephemeral=True)

@simcity_group.command(name="trigger", description="Fire a heartbeat post immediately")
async def simcity_trigger(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    db.set_last_run("sim_city_heartbeat", 0)
    channel = None
    for guild in bot.guilds:
        channel = discord.utils.get(guild.text_channels, name="sim-city")
        if channel:
            break
    if not channel:
        await interaction.followup.send("· sim-city channel not found ·", ephemeral=True)
        return
    whitelist = db.get_sim_personas()
    pool = [p for p in (whitelist if whitelist else list_personas())
            if not chess_engine.is_any_chess_persona(p)]
    persona_name = random.choice(pool)
    sim_topic = db.get_sim_setting("topic")
    system = get_system_prompt(persona_name, channel.id, sim_city_topic=sim_topic, sim_city=True)
    recent_rows = db._conn.execute(
        "SELECT role, content FROM messages WHERE channel_id = ? ORDER BY discord_msg_id DESC LIMIT 5",
        (channel.id,)
    ).fetchall()
    recent_context = "\n".join(f"{r['role']}: {r['content']}" for r in reversed(recent_rows))
    context_block = f"\n\n[Recent channel activity]\n{recent_context}" if recent_context else ""
    prompt = (
        f"The simulation is running. You are {persona_name}. Post something — "
        "a thought, a reaction, a provocation, a question. You may summon another persona, "
        "queue a conversation, or update the scene. Act from your voice."
        f"{context_block}"
    )
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    await process_llm_request(channel, messages, persona_name, None, agent_tools=True)
    await interaction.followup.send(f"· triggered **{persona_name}** ·", ephemeral=True)

@simcity_group.command(name="personas", description="View or manage which personas appear in sim-city")
@app_commands.describe(add="Add a persona to the whitelist", remove="Remove a persona", clear="Clear whitelist (allow all)")
async def simcity_personas_cmd(interaction: discord.Interaction, add: str = "", remove: str = "", clear: bool = False):
    current = db.get_sim_personas() or []
    if clear:
        db.set_sim_personas(None)
        await interaction.response.send_message("· whitelist cleared — all personas active ·", ephemeral=True)
        return
    if add and add not in current:
        current.append(add)
        db.set_sim_personas(current)
    if remove and remove in current:
        current.remove(remove)
        db.set_sim_personas(current if current else None)
    listing = ", ".join(db.get_sim_personas() or ["all"])
    await interaction.response.send_message(f"· sim-city personas: **{listing}** ·", ephemeral=True)

@simcity_group.command(name="queue", description="View the pending conversation queue")
async def simcity_queue_cmd(interaction: discord.Interaction):
    items = db.list_queue()
    if not items:
        await interaction.response.send_message("· queue is empty ·", ephemeral=True)
        return
    lines = [f"{i+1}. **{it['from_persona']}** → **{it['to_persona']}**: {it['seed_prompt'][:60]}" for i, it in enumerate(items)]
    await interaction.response.send_message("· **conversation queue** ·\n" + "\n".join(lines), ephemeral=True)

@simcity_group.command(name="clearqueue", description="Clear the conversation queue")
async def simcity_clearqueue_cmd(interaction: discord.Interaction):
    db.clear_queue()
    await interaction.response.send_message("· queue cleared ·", ephemeral=True)

@simcity_group.command(name="scene", description="Trigger a multi-persona scene reaction")
@app_commands.describe(text="The scenario all personas will react to", count="How many personas (2-5, default 3)")
async def simcity_scene_cmd(interaction: discord.Interaction, text: str, count: int = 3):
    count = max(2, min(5, count))
    await interaction.response.send_message(f"· setting the scene · {count} voices incoming ·", ephemeral=True)
    channel = None
    for guild in bot.guilds:
        channel = discord.utils.get(guild.text_channels, name="sim-city")
        if channel:
            break
    if not channel:
        return
    whitelist = db.get_sim_personas()
    pool = [p for p in (whitelist if whitelist else list_personas())
            if not chess_engine.is_any_chess_persona(p)]
    chosen = random.sample(pool, min(count, len(pool)))
    sim_topic = db.get_sim_setting("topic")
    prev_content = None
    prev_msg_id = None
    for persona_name in chosen:
        if prev_content:
            prompt = f"[scene: {text}]\n\nThe previous message was: {prev_content}\n\nRespond."
        else:
            prompt = f"[scene: {text}]\n\nYou are the first to react."
        system = get_system_prompt(persona_name, channel.id, sim_city_topic=sim_topic, sim_city=True)
        msgs = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
        await process_llm_request(channel, msgs, persona_name, prev_msg_id, agent_tools=True)
        await asyncio.sleep(1)

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

    sim_topic = db.get_sim_setting("topic") if is_sim_city else None
    system = get_system_prompt(persona, message.channel.id, mentions_map=mentions_map, sim_city_topic=sim_topic, sim_city=is_sim_city)
    if is_chess:
        fen_now = chess_engine.current_fen(message.channel.id)
        status = chess_engine.game_status(message.channel.id)
        system += (
            f"\n\n## Chess Board State\nFEN: `{fen_now}`\n"
            f"Legal moves: {chess_engine.legal_moves_str(message.channel.id)}"
        )
        if status: system += f"\n**Game over: {status}**"

    image_blocks = await llm.format_image_blocks(message.attachments)
    # Save to DB: tag messages that had images so history reflects it
    db_content = (f"[image] {prompt}" if prompt else "[image]") if image_blocks else (prompt or " ")
    db.save_message(message.id, parent_id, message.channel.id, "user", db_content, author_id=message.author.id)
    user_content = ([{"type": "text", "text": prompt or " "}] + image_blocks) if image_blocks else (prompt or " ")
    messages_payload = [{"role": "system", "content": system}] + chain + [{"role": "user", "content": user_content}]
    temp = db.get_channel_temperature(message.channel.id)

    if is_sim_city:
        await process_llm_request(message.channel, messages_payload, persona, message.id,
                                  reply_to=message, temperature=temp, mentions_map=mentions_map,
                                  agent_tools=True, has_images=bool(image_blocks))
    else:
        async with message.channel.typing():
            await process_llm_request(message.channel, messages_payload, persona, message.id,
                                      reply_to=message, temperature=temp, mentions_map=mentions_map,
                                      has_images=bool(image_blocks))

@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)
    # Sync guild-scoped slash commands on startup (instant, no propagation delay)
    for guild in bot.guilds:
        try:
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        except Exception as e:
            log.error(f"Failed to sync commands to {guild.name}: {e}")
    log.info(f"Slash commands synced to {len(bot.guilds)} guild(s).")
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
