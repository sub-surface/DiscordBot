"""The heartbeat: a few times a day the bot drops something into chosen channels on its own.

Each channel gets `per_day` slots inside a UTC window, spread out and fixed by the date (so a restart
doesn't reshuffle them). At a slot, Jev reads the last messages and picks the persona whose voice fits:

  live channel     (a member spoke in the last 90 minutes) if there's an opening, that persona chimes in once.
  gone quiet       (the last member message is under 18 hours old) if the chat stopped on a loose end, such
                   as a question nobody answered, that persona picks it back up, linking the message it answers.
  dead channel     nothing. A bot posting openers into an empty room is a bot talking to itself.

No opening, no message. Drops are stored as that persona's answers, so replying to one continues with it.
To keep them from reading alike, a drop-in takes one of a few shapes, the number of slots drifts a little
day to day, and the same Jev call says which piece of server lore (lore.json), if any, the chat calls back
to; only then is the persona handed it.

Two drops in three skip the model (and the GPU) and try a free one first, in random order:

  a stock line     Jev picks one of the persona's own `says` lines that fits the latest message, posted as is
  a receipt        a 🔮 prediction open for at least a week, resurfaced once, linked, to be settled
  a vibe check     the /quick vibe card on the live chat, posted by the persona

Only if none of them lands does the persona write something.
"""

from __future__ import annotations

import json
import logging
import random
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING

import discord

from . import conversation, quick
from .personas import Persona
from .responder import recent_messages
from .speak import generate, speak

if TYPE_CHECKING:
    from .bot import PsychographBot

log = logging.getLogger("psychograph.heartbeat")

SLOT_GRACE = timedelta(minutes=15)     # a slot fires if the loop sees it within this long
ACTIVE_WITHIN = timedelta(minutes=90)  # a member message this recent makes the channel live
QUIET_WITHIN = timedelta(hours=18)     # …and older than this, the channel is left alone
OPENING = 0.5                          # Jev's yes needed to interrupt a live channel
LOOSE_END = 0.6                        # …or to pick up a quiet one
CONTEXT_MESSAGES = 25
LORE = 0.7                             # Jev's confidence needed to hand the persona a piece of lore
NO_LORE = "none"
FREE_SHARE = 2 / 3                     # drops that try the free kinds before the model
STOCK_LINE = 0.8                       # Jev's confidence needed to post a `says` line as is
RECEIPT_AGE = timedelta(days=7)        # a prediction this old is due for its receipt

# What a drop-in does, picked at random so a day's drops don't all read the same.
SHAPES = (
    "react to the conversation above",
    "pick out one specific line above and riff on it",
    "ask one person above a pointed question",
    "push the topic above somewhere it hasn't gone yet",
    "side with someone above, or against them",
)

OPENING_QUESTION = {
    "type": "noul",
    "instructions": "Is there a natural opening in `messages` for a funny or interesting comment from "
    "someone who hasn't spoken yet, without interrupting something serious or private?",
}
LOOSE_END_QUESTION = {
    "type": "noul",
    "instructions": "Did `messages` stop on a loose end someone could still pick up: a question nobody "
    "answered, a claim nobody answered, or a bit left hanging?",
    "criteria": {
        "true": "The last messages ask or claim something that got no answer and would still be fun to answer.",
        "false": "The conversation wrapped up, said goodbye, or ended on something serious, private or settled.",
    },
}


def slots(day: date, channel: str, per_day: int, start_hour: int, end_hour: int) -> list[datetime]:
    """About `per_day` times (one either way, by the day) in the UTC window [start_hour, end_hour), wrapping
    midnight, one per equal segment."""
    rng = random.Random(f"{day.isoformat()}:{channel}")
    count = max(1, per_day + rng.choice((-1, 0, 0, 1))) if per_day > 0 else 0
    span = (end_hour - start_hour) % 24 or 24
    segment = span * 60 / max(count, 1)
    base = datetime.combine(day, time(start_hour), tzinfo=timezone.utc)
    return [base + timedelta(minutes=int(i * segment + rng.uniform(0.15, 0.85) * segment)) for i in range(count)]


def due(now: datetime, channel: str, per_day: int, start_hour: int, end_hour: int) -> list[str]:
    """Keys of the slots that should fire now (checking today and yesterday, for windows past midnight)."""
    keys = []
    for day in (now.date() - timedelta(days=1), now.date()):
        for index, slot in enumerate(slots(day, channel, per_day, start_hour, end_hour)):
            if slot <= now < slot + SLOT_GRACE:
                keys.append(f"{day.isoformat()}:{channel}:{index}")
    return keys


def candidates(bot: PsychographBot, guild_id: int | None) -> list[Persona]:
    return [p for p in bot.personas.available(guild_id) if p.mode == "chat" and p.group in ("chatters", "characters")]


def load_lore(path) -> dict[str, str]:
    """lore.json: {name: one line of server lore}. Missing or broken means none."""
    try:
        return {str(k): str(v) for k, v in json.loads(path.read_text(encoding="utf-8")).items()}
    except (OSError, ValueError, AttributeError):
        return {}


async def stock_line(bot: PsychographBot, persona: Persona, recent: list) -> str | None:
    """One of the persona's own lines, if Jev is sure it fits as the next message."""
    if not persona.says:
        return None
    answers = await bot.jev.ask(
        {"messages": [{"speaker": conversation.speaker_label(item), "text": item.text[:300]} for item in recent[-12:]]},
        {
            "line": {
                "type": "choice",
                "instructions": f"Which of {persona.name}'s stock lines would land as the next message after "
                "`messages`, as a reply that makes sense? Usually none.",
                "criteria": {**{line: None for line in persona.says}, NO_LORE: "None of them would make sense here."},
            }
        },
    )
    pick = (answers or {}).get("line") or {}
    line = pick.get("choice")
    return line if line in persona.says and pick.get("confidence", 0) >= STOCK_LINE else None


def receipt(bot: PsychographBot, guild_id: int | None) -> str | None:
    """The oldest open prediction due for a receipt it hasn't had yet, marked as resurfaced."""
    if guild_id is None:
        return None
    due_by = datetime.now(timezone.utc) - RECEIPT_AGE
    for item in reversed(bot.store.predictions(guild_id, limit=50)):
        key = f"receipt:{item['message_id']}"
        if discord.utils.snowflake_time(item["message_id"]) > due_by or bot.store.state(key):
            continue
        bot.store.set_state(key, "1")
        when = discord.utils.snowflake_time(item["message_id"])
        link = f"https://discord.com/channels/{guild_id}/{item['channel_id']}/{item['message_id']}"
        quote = item["text"][:300].replace("\n", " ")
        return (
            f"-# 🔮 receipt due\n> {quote}\n<@{item['author_id']}>, [{when.day} {when:%b}](<{link}>). "
            "Did it happen? Settle it with `/scores predictions`."
        )
    return None


async def vibe_card(bot: PsychographBot, persona: Persona, recent: list) -> str | None:
    """The /quick vibe card on the live chat, as text."""
    embed = await quick.vibe(bot.jev, quick.QuickRequest(text="", speaker=persona.name, said=recent))
    if not (embed.title or "").startswith("Vibe check ·"):
        return None
    return f"-# {embed.title.lower()}\n{embed.description}"


async def free_drop(bot: PsychographBot, persona: Persona, recent: list, live: bool, guild_id: int | None, rng: random.Random):
    """(text, kind) from the first free kind that lands, tried in random order; None if none does."""
    kinds = ["line", "receipt"] + (["vibe"] if live else [])
    rng.shuffle(kinds)
    for kind in kinds:
        if kind == "line":
            text = await stock_line(bot, persona, recent)
        elif kind == "receipt":
            text = receipt(bot, guild_id)
        else:
            text = await vibe_card(bot, persona, recent)
        if text:
            return text, kind
    return None


def _ago(delta: timedelta) -> str:
    hours = delta.total_seconds() / 3600
    return f"{hours:.0f} hours" if hours >= 1.5 else f"{delta.total_seconds() / 60:.0f} minutes"


async def drop(bot: PsychographBot, channel: discord.TextChannel, rng: random.Random | None = None) -> discord.Message | None:
    """One heartbeat in `channel`: a drop-in on live chat, or a pick-up of a loose end once it's gone quiet."""
    rng = rng or random.Random()
    guild_id = channel.guild.id if getattr(channel, "guild", None) else None
    personas = candidates(bot, guild_id)
    said = await recent_messages(channel, limit=40)
    members = [item for item in said if not item.by_bot]
    if not personas or not members or members[-1].created_at is None:
        return None
    last = members[-1]
    quiet = datetime.now(timezone.utc) - last.created_at
    if quiet >= QUIET_WITHIN:
        log.info("Heartbeat in #%s: nobody around for %s", channel.name, _ago(quiet))
        return None
    live = quiet < ACTIVE_WITHIN

    recent = said[-CONTEXT_MESSAGES:]
    lore = load_lore(bot.settings.lore_file)
    questions = {
        "opening": OPENING_QUESTION if live else LOOSE_END_QUESTION,
        "who": {
            "type": "choice",
            "instructions": "Whose voice would add the funniest or most fitting comment to `messages`?",
            "criteria": {p.name: (p.compact_prompt or p.prompt)[:300] for p in personas},
        },
    }
    if lore:
        questions["lore"] = {
            "type": "choice",
            "instructions": "Which piece of server lore do `messages` genuinely call back to? Usually none.",
            "criteria": {**lore, NO_LORE: "Nothing here clearly connects to any of them."},
        }
    answers = await bot.jev.ask(
        {"messages": [{"speaker": conversation.speaker_label(item), "text": item.text[:300]} for item in recent]},
        questions,
    )
    if not answers or answers.get("opening", {}).get("noul", 0) < (OPENING if live else LOOSE_END):
        log.info("Heartbeat in #%s: %s", channel.name, "no opening" if live else "no loose end")
        return None
    persona = next((p for p in personas if p.name == answers.get("who", {}).get("choice")), rng.choice(personas))
    if rng.random() < FREE_SHARE and (free := await free_drop(bot, persona, recent, live, guild_id, rng)):
        text, kind = free
        message = await speak(bot, channel, persona, text)
        if message is not None:
            bot.store.save_message(message.id, None, channel.id, "assistant", text, answer_id=message.id, persona=persona.key)
            log.info("Heartbeat in #%s: %s posted a %s (no model)", channel.name, persona.name, kind)
        return message
    context = conversation.channel_transcript(
        recent, conversation.CHAT_TRANSCRIPT_HEADER, you=persona.name, limit=conversation.CHAT_TRANSCRIPT_LINE_CHARS
    )
    if live:
        instruction = (
            f"Chime in once, unprompted, as {persona.name}: one or two short lines; {rng.choice(SHAPES)}. "
            "Don't greet anyone, don't explain why you're here, and don't write anyone else's lines."
        )
    else:
        instruction = (
            f"The chat above went quiet {_ago(quiet)} ago, on a loose end nobody picked up. Pick it back up as "
            f"{persona.name}: answer it or push it further, in one or two short lines. Don't greet anyone, don't "
            "mention the silence, and don't write anyone else's lines."
        )
    pick = answers.get("lore") or {}
    callback = lore.get(pick.get("choice", NO_LORE)) if pick.get("confidence", 0) >= LORE else None
    if callback:
        instruction += f" Server lore this touches on, if you can work it in naturally: {callback}"
    text = await generate(bot, persona, instruction, context, sorted({item.speaker for item in members}))
    if not text:
        return None
    posted = text
    if not live and last.message_id is not None and guild_id is not None:
        link = f"https://discord.com/channels/{guild_id}/{channel.id}/{last.message_id}"
        posted = f"-# ↪ [{last.speaker}](<{link}>)\n{text}"   # webhooks can't reply, so point at it
    message = await speak(bot, channel, persona, posted)
    if message is not None:
        bot.store.save_message(message.id, None, channel.id, "assistant", text, answer_id=message.id, persona=persona.key)
        log.info("Heartbeat in #%s: %s %s", channel.name, persona.name, "chimed in" if live else "picked up a loose end")
    return message
