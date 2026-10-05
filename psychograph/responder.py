"""Answering one request end to end: context → model → clean → deliver → record.

Shared by mentions, replies, /ask, the "Ask persona" message command and 🔁 regenerate.
Status reactions on the request show progress: 👀 thinking, ☁️ waking a cold GPU,
⏳ queued behind another reply, ⚠️ failed.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord

from . import conversation, render
from .backends import Completion
from .personas import Persona
from .store import Generation

if TYPE_CHECKING:
    from .bot import PsychographBot

log = logging.getLogger("psychograph.responder")

THINKING, COLD, QUEUED, FAILED = "👀", "☁️", "⏳", "⚠️"
FALLBACK_REPLY = "I don't have a response for that."
_SPEAKER = re.compile(r"^([^:\n]{1,40}):")


@dataclass
class Ask:
    """One request to answer."""

    channel: discord.abc.Messageable
    guild_id: int | None
    author: discord.abc.User          # whose words these are (named in the prompt)
    requester_id: int                 # who may regenerate or delete the answer
    text: str
    record_id: int                    # store id for the user turn (a message or interaction id)
    parent_id: int | None = None      # the message this continues, for reply-chain history
    reply_to: discord.Message | None = None
    embeds: Sequence[discord.Embed] = ()
    mentions: Sequence[discord.abc.User] = ()
    persona: Persona | None = None    # a one-off persona (for /ask); default is the channel's
    followup: discord.Webhook | None = None  # deliver as interaction followups instead


@dataclass
class Prepared:
    persona: Persona
    messages: list[dict]
    source_urls: list[str]
    recipient: discord.abc.User | None
    notice: str | None
    speakers: list[str] = field(default_factory=list)


async def _react(message: discord.Message | None, emoji: str) -> None:
    if message is None:
        return
    try:
        await message.add_reaction(emoji)
    except discord.HTTPException:
        pass


async def _unreact(message: discord.Message | None, emoji: str, me: discord.abc.User | None) -> None:
    if message is None or me is None:
        return
    try:
        await message.remove_reaction(emoji, me)
    except discord.HTTPException:
        pass


class Responder:
    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot

    async def prepare(self, ask: Ask) -> Prepared:
        bot, backend = self.bot, self.bot.backend
        compact = backend.profile.compact
        persona = ask.persona or bot.personas.for_channel(ask.channel.id, ask.guild_id)
        tweets = await conversation.tweet_context(ask.text, ask.embeds)
        me_id = bot.user.id if bot.user else 0
        recipient = conversation.addressed_member(ask.text, ask.mentions, me_id)
        speaker = getattr(ask.author, "display_name", None) or ask.author.name
        turn = conversation.user_turn(ask.text, tweets, recipient, speaker, compact=compact)

        history = (
            bot.store.message_chain(ask.parent_id, ask.channel.id, limit=backend.profile.history_messages)
            if ask.parent_id
            else []
        )
        if compact:
            history = conversation.compact_history(history)
        bot.store.save_message(ask.record_id, ask.parent_id, ask.channel.id, "user", turn, ask.author.id)

        verbosity = bot.store.channel_settings(ask.channel.id).verbosity
        messages, notice = conversation.fit_context(
            conversation.system_prompt(persona, verbosity, compact=compact),
            history,
            turn,
            backend.context_limit,
            backend.output_limit,
        )
        speakers = {speaker}
        speakers.update(match.group(1) for item in history if item["role"] == "user" and (match := _speaker_of(item)))
        return Prepared(persona, messages, [url for url, _text in tweets], recipient, notice, sorted(speakers))

    async def answer(self, ask: Ask) -> list[discord.Message]:
        """Generate and deliver an answer; returns the posted messages (empty on failure)."""
        backend = self.bot.backend
        status = QUEUED if backend.in_flight else COLD if backend.likely_cold else THINKING
        cold = status == COLD
        await _react(ask.reply_to, status)
        started = time.perf_counter()
        prepared: Prepared | None = None
        try:
            async with ask.channel.typing():
                prepared = await self.prepare(ask)
                completion = await backend.complete(prepared.messages)
            wall = time.perf_counter() - started
            text = conversation.clean_reply(completion.text, prepared.persona.name, prepared.speakers) or FALLBACK_REPLY
            sent = await self._deliver(ask, prepared, text, render.generation_summary(completion, wall))
            self._record(ask, prepared, completion, wall, cold, ok=True)
        except Exception as error:
            log.exception("Response failed in channel %s", ask.channel.id)
            self._record(ask, prepared, None, time.perf_counter() - started, cold, ok=False, error=repr(error)[:300])
            await _unreact(ask.reply_to, status, self.bot.user)
            await self._report_failure(ask)
            return []
        await _unreact(ask.reply_to, status, self.bot.user)
        if sent and self.bot.store.channel_settings(ask.channel.id).persona_reactions:
            await _react(sent[0], prepared.persona.reaction)
        return sent

    async def _deliver(self, ask: Ask, prepared: Prepared, text: str, summary: str) -> list[discord.Message]:
        store, persona, recipient = self.bot.store, prepared.persona, prepared.recipient
        mentions = discord.AllowedMentions(
            users=[recipient] if recipient else [], roles=False, everyone=False, replied_user=False
        )
        mention = f"{recipient.mention} " if recipient else ""
        voice = (
            ask.followup is None
            and store.channel_settings(ask.channel.id).persona_voice
            and self.bot.webhooks.available(ask.channel)
        )

        sent: list[discord.Message] = []
        if voice:
            chunks, posted = render.voice_messages(text, prepared.source_urls, prepared.notice)
            if ask.reply_to is not None:
                who = getattr(ask.author, "display_name", ask.author.name)
                posted[0] = f"-# ↪ [{who}](<{ask.reply_to.jump_url}>)\n{mention}{posted[0]}"
            elif mention:
                posted[0] = mention + posted[0]
            for content in posted:
                message = await self.bot.webhooks.send(ask.channel, persona, content, mentions)
                if message is None:  # webhook unavailable after all
                    break
                sent.append(message)
            voice = bool(sent)
        if not voice:
            chunks, embeds = render.response_embeds(
                text, persona.name, prepared.source_urls, (summary, prepared.notice), persona.avatar_url
            )
            for index, embed in enumerate(embeds):
                content = (mention.strip() or None) if index == 0 else None
                if ask.followup is not None:
                    message = await ask.followup.send(content=content, embed=embed, allowed_mentions=mentions, wait=True)
                elif index == 0 and ask.reply_to is not None:
                    message = await ask.reply_to.reply(content=content, embed=embed, allowed_mentions=mentions)
                else:
                    message = await ask.channel.send(
                        content=content,
                        embed=embed,
                        reference=sent[-1] if sent else None,
                        allowed_mentions=mentions if index == 0 else discord.AllowedMentions.none(),
                    )
                sent.append(message)

        parent_id = ask.record_id
        for chunk, message in zip(chunks, sent):
            store.save_message(message.id, parent_id, ask.channel.id, "assistant", chunk, reply_to=ask.record_id)
            parent_id = message.id
        return sent

    async def _report_failure(self, ask: Ask) -> None:
        text = "I couldn't reach the model. Check the bot and model server logs, or react 🔁 to try again."
        try:
            if ask.followup is not None:
                await ask.followup.send(text, ephemeral=True)
            elif ask.reply_to is not None:
                await _react(ask.reply_to, FAILED)
                await ask.reply_to.reply(text, mention_author=False, delete_after=60)
            else:
                await ask.channel.send(text, delete_after=60)
        except discord.HTTPException:
            log.info("Couldn't report the failure in channel %s", ask.channel.id)

    def _record(
        self,
        ask: Ask,
        prepared: Prepared | None,
        completion: Completion | None,
        wall: float,
        cold: bool,
        ok: bool,
        error: str | None = None,
    ) -> None:
        backend = self.bot.backend
        persona = prepared.persona.name if prepared else (ask.persona.name if ask.persona else "?")
        try:
            self.bot.store.record_generation(
                Generation(
                    channel_id=ask.channel.id,
                    guild_id=ask.guild_id,
                    persona=persona,
                    model=backend.label,
                    profile=backend.profile.key,
                    ok=ok,
                    wall_seconds=round(wall, 3),
                    completion_tokens=completion.tokens if completion else None,
                    prompt_tokens=conversation.estimate_tokens(prepared.messages) if prepared else None,
                    tokens_per_second=completion.tokens_per_second if completion else None,
                    cold_start=cold,
                    trimmed=bool(prepared and prepared.notice and "trimmed" in prepared.notice),
                    error=error,
                )
            )
        except Exception:
            log.exception("Couldn't record generation stats")

    async def delete_response(self, channel: discord.abc.Messageable, request_id: int) -> int:
        """Delete every message answering `request_id`, in Discord and in history. Returns how many."""
        ids = self.bot.store.response_ids(request_id)
        for message_id in ids:
            if await self.bot.webhooks.delete(channel, message_id):
                continue
            try:
                await channel.get_partial_message(message_id).delete()
            except discord.HTTPException:
                log.info("Couldn't delete message %s", message_id)
        self.bot.store.delete_messages(ids)
        return len(ids)


def _speaker_of(item: dict) -> re.Match[str] | None:
    """The speaker name at the start of a stored user turn ("Name: text")."""
    return _SPEAKER.match(str(item["content"]))
