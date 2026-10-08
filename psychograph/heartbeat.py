"""The heartbeat: a few times a day the bot drops something into chosen channels on its own.

Each channel gets `per_day` slots inside a UTC window, spread out and fixed by the date (so a restart
doesn't reshuffle them). At a slot, Jev reads the last messages and picks the persona whose voice fits:

  live channel     (a member spoke in the last 90 minutes) if there's an opening, that persona chimes in once.
  gone quiet       (the last member message is under 18 hours old) if the chat stopped on a loose end, such
                   as a question nobody answered, that persona picks it back up, linking the message it answers.
  dead channel     nothing. A bot posting openers into an empty room is a bot talking to itself.

No opening, no message. Drops are stored as that persona's answers, so replying to one continues with it.
"""

from __future__ import annotations

import logging
import random
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING

import discord

from . import conversation
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
    """`per_day` times in the UTC window [start_hour, end_hour) (wrapping midnight), one per equal segment."""
    span = (end_hour - start_hour) % 24 or 24
    segment = span * 60 / max(per_day, 1)
    rng = random.Random(f"{day.isoformat()}:{channel}")
    base = datetime.combine(day, time(start_hour), tzinfo=timezone.utc)
    return [base + timedelta(minutes=int(i * segment + rng.uniform(0.15, 0.85) * segment)) for i in range(per_day)]


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
    answers = await bot.jev.ask(
        {"messages": [{"speaker": conversation.speaker_label(item), "text": item.text[:300]} for item in recent]},
        {
            "opening": OPENING_QUESTION if live else LOOSE_END_QUESTION,
            "who": {
                "type": "choice",
                "instructions": "Whose voice would add the funniest or most fitting comment to `messages`?",
                "criteria": {p.name: (p.compact_prompt or p.prompt)[:300] for p in personas},
            },
        },
    )
    if not answers or answers.get("opening", {}).get("noul", 0) < (OPENING if live else LOOSE_END):
        log.info("Heartbeat in #%s: %s", channel.name, "no opening" if live else "no loose end")
        return None
    persona = next((p for p in personas if p.name == answers.get("who", {}).get("choice")), rng.choice(personas))
    context = conversation.channel_transcript(
        recent, conversation.CHAT_TRANSCRIPT_HEADER, you=persona.name, limit=conversation.CHAT_TRANSCRIPT_LINE_CHARS
    )
    if live:
        instruction = (
            f"Chime in once, unprompted, as {persona.name}: one or two short lines reacting to the conversation "
            "above. Don't greet anyone, don't explain why you're here, and don't write anyone else's lines."
        )
    else:
        instruction = (
            f"The chat above went quiet {_ago(quiet)} ago, on a loose end nobody picked up. Pick it back up as "
            f"{persona.name}: answer it or push it further, in one or two short lines. Don't greet anyone, don't "
            "mention the silence, and don't write anyone else's lines."
        )
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
