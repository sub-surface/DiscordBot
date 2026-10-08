"""Answering one request end to end: context → model → clean → deliver → record.

Shared by mentions, replies, /ask, the message commands and 🔁 regenerate.
Status reactions on the request show progress: a random server "thinking" emote (else 👀), ☁️ waking a cold GPU,
⏳ queued behind another reply, ⚠️ failed.

Tool personas read the channel's recent messages and only run on models whose profile allows
tools. A tool with a System 1 pass (Jev) runs it first: the debate review is screened for an
actual argument (if there's none it answers at once, without the model) and gets per-message
foul tags as hints; afterwards Jev reads the decision back for the leaderboard.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import discord

from . import conversation, render, sounds
from .backends import Completion
from .conversation import Said
from .jev import NO_CONTEST, SPLIT, DebateNotes
from .personas import Persona
from .sounds import Sound
from .store import Generation

if TYPE_CHECKING:
    from .bot import PsychographBot

log = logging.getLogger("psychograph.responder")

THINKING, COLD, QUEUED, FAILED = "👀", "☁️", "⏳", "⚠️"
FALLBACK_REPLY = "I don't have a response for that."
NO_DEBATE_REPLY = (
    "**No contest.** I've read the recent messages and nobody is arguing opposing positions — "
    "the court needs at least two people disagreeing. If the argument was further back, right-click "
    "its last message → **Apps → Review this debate**."
)
RECENT_MESSAGES = 100   # one Discord history page: enough for any tool's transcript and for triage
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
    persona: Persona | None = None    # a one-off persona (/ask, a tool called by name); default is the channel's
    followup: discord.Webhook | None = None  # deliver as interaction followups instead
    include_reply_to: bool = False    # the transcript ends with reply_to itself ("Review this debate")
    shown_status: str | discord.Emoji | None = None  # a status reaction the caller already put on reply_to
    recent: list[Said] | None = None  # the channel's recent messages, fetched once and shared
    chain: list[dict] | None = None   # the stored reply chain this continues, read once and shared
    window: list[Said] | None = None  # context candidates for a chat persona: recent, minus its reply chain
    keep: tuple[int, ...] = ()        # the window messages System 1 judged relevant to this request


@dataclass
class Prepared:
    persona: Persona
    messages: list[dict]
    source_urls: list[str]
    recipient: discord.abc.User | None
    notice: str | None
    speakers: list[str] = field(default_factory=list)
    sounds_on: bool = False   # the soundbank menu was offered to the model
    voiced: bool = False      # the answer went out through the persona's webhook
    said: list[Said] = field(default_factory=list)  # the channel transcript a tool read
    notes: DebateNotes | None = None                 # System 1's debate screening
    canned: str | None = None                        # System 1 answered; skip the model
    context_count: int = 0                           # channel messages a chat persona was shown


async def recent_messages(
    channel: discord.abc.Messageable, before: discord.Message | None = None, through: bool = False,
    limit: int = RECENT_MESSAGES,
) -> list[Said]:
    """A channel's messages before `before` (and `before` itself if `through`), oldest first."""
    history = getattr(channel, "history", None)
    if history is None:
        return []
    through = through and before is not None
    try:
        messages = [message async for message in history(limit=limit - through, before=before)]
    except discord.HTTPException:
        log.info("Couldn't read recent messages in channel %s", getattr(channel, "id", "?"))
        return []
    messages.reverse()
    if through:
        messages.append(before)
    return [item for message in messages if (item := conversation.said(message))]


async def react(message: discord.Message | None, emoji: str | discord.Emoji) -> None:
    if message is None:
        return
    try:
        await message.add_reaction(emoji)
    except discord.HTTPException as error:
        # Usually a missing Add Reactions permission in that channel; log it so it isn't invisible.
        log.info("Couldn't react %s in channel %s: %s", emoji, getattr(message.channel, "id", "?"), error)


async def unreact(message: discord.Message | None, emoji: str | discord.Emoji | None, me: discord.abc.User | None) -> None:
    if message is None or me is None or not emoji:
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

        history = conversation.quote_other_personas(self.chain(ask), persona.key, lambda key: self._name_of(ask.guild_id, key))
        if compact:
            history = conversation.compact_history(history)
        bot.store.save_message(ask.record_id, ask.parent_id, ask.channel.id, "user", turn, ask.author.id)

        # Tool personas read the channel's recent messages, fetched fresh each time and never stored,
        # so a follow-up reply sees the conversation as it is now rather than piling up old transcripts.
        prompt, recent, notes, canned = turn, [], None, None
        channel_speakers: set[str] = set()
        context_count = 0
        if persona.channel_context:
            recent = (await self.recent(ask))[-persona.channel_context:]
            parts = [conversation.channel_transcript(recent)]
            if persona.system1 == "debate" and recent:
                notes = await bot.jev.debate_notes(recent)
                if notes is not None and notes.no_debate:
                    canned = NO_DEBATE_REPLY
                elif notes is not None:
                    parts.append(notes.prompt_block())
            prompt = "\n\n".join(part for part in (*parts, turn) if part)
        elif backend.profile.channel_messages and persona.mode == "chat":
            # Chat personas see the channel messages System 1 judged relevant to this request (plus the
            # last few), not the whole channel: less to read, and less to confuse.
            if ask.window is None:
                await self.triage(ask, {})
            context = [ask.window[index] for index in ask.keep if index < len(ask.window)]
            transcript = conversation.channel_transcript(
                context, conversation.CHAT_TRANSCRIPT_HEADER, you=persona.name, limit=conversation.CHAT_TRANSCRIPT_LINE_CHARS
            )
            if transcript:
                prompt = f"{transcript}\n\n{conversation.REPLY_MARKER}\n{turn}"
            channel_speakers = {item.speaker for item in context if not item.by_bot}
            context_count = len(context)

        settings = bot.store.channel_settings(ask.channel.id)
        tool = persona.group == "tools"
        sounds_on = settings.sounds and len(bot.soundbank) > 0 and not tool
        system = conversation.system_prompt(persona, settings.verbosity, compact=compact)
        if sounds_on:
            system = f"{system}\n\n{bot.soundbank.menu(compact)}"
        messages, notice = conversation.fit_context(
            system, history, prompt, backend.context_limit, backend.output_limit
        )
        # Tools write reports that name people ("**Leon:** strong opening"), which the invented-turn cut
        # would mistake for a new speaker; it's only there for chat models that run on as someone else.
        speakers = {speaker, *channel_speakers}
        speakers.update(
            match.group(1)
            for item in history
            if item["role"] == "user" and not item.get("quoted") and (match := _speaker_of(item))
        )
        return Prepared(
            persona, messages, [url for url, _text in tweets], recipient, notice,
            [] if tool else sorted(speakers), sounds_on, said=recent, notes=notes, canned=canned,
            context_count=context_count,
        )

    async def recent(self, ask: Ask) -> list[Said]:
        """The channel's recent messages for this request, fetched once."""
        if ask.recent is None:
            ask.recent = await recent_messages(ask.channel, ask.reply_to, ask.include_reply_to)
        return ask.recent

    def chain(self, ask: Ask) -> list[dict]:
        """The stored reply chain this request continues, read once."""
        if ask.chain is None:
            limit = self.bot.backend.profile.history_messages
            ask.chain = self.bot.store.message_chain(ask.parent_id, ask.channel.id, limit=limit) if ask.parent_id else []
        return ask.chain

    async def triage(self, ask: Ask, routes: dict[str, str]) -> str | None:
        """System 1 for a request, in one Jev call: the route it asks for among `routes` (None means chat), and
        which recent channel messages a chat persona should see (set on the ask as window and keep)."""
        profile = self.bot.backend.profile
        window: list[Said] = []
        if profile.channel_messages:
            chain_ids = {row["discord_msg_id"] for row in self.chain(ask)}
            window = [item for item in (await self.recent(ask))[-profile.channel_messages:] if item.message_id not in chain_ids]
        speaker = getattr(ask.author, "display_name", None) or ask.author.name
        result = await self.bot.jev.triage(ask.text, speaker, window, routes)
        ask.window, ask.keep = window, result.keep
        return result.kind

    def _name_of(self, guild_id: int | None, key: str) -> str:
        if key.startswith("quick:"):
            return f"Jev, {key.removeprefix('quick:')}"
        persona = self.bot.personas.get(guild_id, key)
        return persona.name if persona else key.replace("_", " ")

    def can_run(self, persona: Persona) -> bool:
        """Tool personas need a model whose profile allows them (weaker models derail on long transcripts)."""
        return persona.group != "tools" or persona.mode != "chat" or self.bot.backend.profile.tools

    def tools_unavailable(self, persona: Persona) -> str:
        backend = self.bot.backend
        model = backend.profile.name.split(" (")[0] if backend.profile.key != "default" else backend.label
        return (
            f"🛠️ **{persona.name}** needs a stronger model than **{model}**. "
            "Tools run on MiMo — switch with the dashboard's *Choose model*."
        )

    def status(self, guild: discord.Guild | None = None) -> str | discord.Emoji:
        """The status reaction for a new request: ⏳ queued, ☁️ cold GPU, else a random thinking emote (or 👀)."""
        backend = self.bot.backend
        if backend.in_flight:
            return QUEUED
        if backend.likely_cold:
            return COLD
        return self.bot.ambient.thinking(guild) or THINKING

    async def answer(self, ask: Ask) -> list[discord.Message]:
        """Generate and deliver an answer; returns the posted messages (empty on failure)."""
        ask.persona = ask.persona or self.bot.personas.for_channel(ask.channel.id, ask.guild_id)
        if not self.can_run(ask.persona):
            await unreact(ask.reply_to, ask.shown_status, self.bot.user)
            await self._report(ask, self.tools_unavailable(ask.persona))
            return []
        backend = self.bot.backend
        status = ask.shown_status or self.status(getattr(ask.channel, "guild", None))
        cold = status == COLD
        if ask.shown_status is None:
            await react(ask.reply_to, status)
        started = time.perf_counter()
        prepared: Prepared | None = None
        tagged: Sound | None = None
        try:
            async with ask.channel.typing():
                prepared = await self.prepare(ask)
                completion = None if prepared.canned else await backend.complete(prepared.messages)
            wall = time.perf_counter() - started
            if completion is None:
                text, summary = prepared.canned, f"System 1 (Jev) · {wall:.1f}s · no model call"
            else:
                raw, tagged = self.bot.soundbank.extract(completion.text) if prepared.sounds_on else (completion.text, None)
                text = conversation.clean_reply(raw, prepared.persona.name, prepared.speakers) or FALLBACK_REPLY
                summary = render.generation_summary(completion, wall)
                if prepared.context_count:
                    summary = f"{summary} · saw {prepared.context_count} channel msgs"
                notes = prepared.notes
                if notes is not None and notes.lean:
                    summary = f"{summary} · System 1 leaned {notes.lean} ({notes.lean_confidence:.0%})"
            sent = await self._deliver(ask, prepared, text, summary)
            if completion is not None:
                self._record(ask, prepared, completion, wall, cold, ok=True)
        except Exception as error:
            log.exception("Response failed in channel %s", ask.channel.id)
            self._record(ask, prepared, None, time.perf_counter() - started, cold, ok=False, error=repr(error)[:300])
            await unreact(ask.reply_to, status, self.bot.user)
            await self._report_failure(ask)
            return []
        await unreact(ask.reply_to, status, self.bot.user)
        if prepared.sounds_on and sent:
            sound = await self._pick_sound(ask.channel.id, tagged, ask.text, text)
            if sound is not None:
                await self._play(ask, prepared, sound, sent[-1])
        if prepared.persona.system1 == "debate" and prepared.canned is None and sent:
            await self._score_debate(ask, prepared, text, sent[0].id)
            notes = prepared.notes
            if notes is not None and (notes.flags or notes.lean):
                # Kept with the review, so a follow-up asking why can see what System 1 flagged.
                self.bot.store.append_to_message(sent[0].id, f"\n\n{notes.record()}")
        return sent

    async def _pick_sound(self, channel_id: int, tagged: Sound | None, request: str, reply: str) -> Sound | None:
        """The model's tagged sound, else Jev's pick for the moment, else (without Jev) a keyword match."""
        bank, jev = self.bot.soundbank, self.bot.jev
        if tagged is None and jev.enabled and bank.ready(channel_id, tagged=False):
            key = await jev.pick_sound(request, reply, list(bank.sounds.values()))
            if key is not None:  # Jev answered; None means it couldn't be asked
                sound = bank.sounds.get(key)
                if sound is not None:
                    bank.played(channel_id)
                return sound
        return bank.choose(channel_id, tagged, request, reply)

    async def _score_debate(self, ask: Ask, prepared: Prepared, review: str, answer_id: int) -> None:
        """Put a review's result on the leaderboard: members it names in bold who spoke in the transcript."""
        people = {item.speaker: item.author_id for item in prepared.said if item.author_id is not None}
        named = [name for name in people if f"**{name}**" in review]
        decision = next((line for line in review.splitlines() if "decision" in line.casefold()), "")
        if len(named) < 2 or not decision:
            return
        jev = self.bot.jev
        outcome = await jev.read_verdict(decision, named) if jev.enabled else read_verdict(decision, named)
        if outcome is None or outcome == NO_CONTEST:
            return
        winner = None if outcome == SPLIT else people[outcome]
        self.bot.store.record_debate(answer_id, ask.guild_id, ask.channel.id, winner, [people[name] for name in named])

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
        prepared.voiced = voice
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
            store.save_message(
                message.id,
                parent_id,
                ask.channel.id,
                "assistant",
                chunk,
                reply_to=ask.record_id,
                requester_id=ask.requester_id,
                answer_id=sent[0].id,
                persona=persona.key,
            )
            parent_id = message.id
        return sent

    async def _play(self, ask: Ask, prepared: Prepared, sound: Sound, answer: discord.Message) -> None:
        """Follow the answer with its sound: from the persona itself in Voice mode, else a voice message."""
        try:
            if prepared.voiced:
                file = discord.File(sound.path, filename=f"{sound.key}.ogg")
                try:
                    await self.bot.webhooks.send(
                        ask.channel, prepared.persona, "", discord.AllowedMentions.none(), file=file
                    )
                finally:
                    file.close()
            else:
                await sounds.play(ask.channel, sound, reference=answer)
        except (discord.HTTPException, OSError):
            log.exception("Couldn't play %s in channel %s", sound.key, ask.channel.id)

    async def _report_failure(self, ask: Ask) -> None:
        await react(ask.reply_to, FAILED)
        await self._report(ask, "I couldn't reach the model. Check the bot and model server logs, or react 🔁 to try again.")

    async def _report(self, ask: Ask, text: str) -> None:
        """A short-lived notice to the asker: ephemeral for commands, a self-deleting reply otherwise."""
        try:
            if ask.followup is not None:
                await ask.followup.send(text, ephemeral=True)
            elif ask.reply_to is not None:
                await ask.reply_to.reply(text, mention_author=False, delete_after=60)
            else:
                await ask.channel.send(text, delete_after=60)
        except discord.HTTPException:
            log.info("Couldn't report to channel %s", ask.channel.id)

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

    async def delete_answer(self, channel: discord.abc.Messageable, message_id: int) -> int:
        """Delete the one answer containing `message_id` (all its chunks), in Discord and history."""
        ids = self.bot.store.answer_ids(message_id)
        for message_id in ids:
            if await self.bot.webhooks.delete(channel, message_id):
                continue
            try:
                await channel.get_partial_message(message_id).delete()
            except discord.HTTPException:
                log.info("Couldn't delete message %s", message_id)
        self.bot.store.delete_messages(ids)
        self.bot.store.delete_debates(ids)
        return len(ids)


def read_verdict(decision: str, participants: Sequence[str]) -> str | None:
    """Without Jev: no contest, split, or whichever participant the decision line names first."""
    lowered = decision.casefold()
    if "no contest" in lowered:
        return NO_CONTEST
    if "split" in lowered or "draw" in lowered:
        return SPLIT
    positions = [(lowered.find(name.casefold()), name) for name in participants if name.casefold() in lowered]
    return min(positions)[1] if positions else None


def _speaker_of(item: dict) -> re.Match[str] | None:
    """The speaker name at the start of a stored user turn ("Name: text")."""
    return _SPEAKER.match(str(item["content"]))
