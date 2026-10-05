"""Replying to messages: mention or reply → build the request → complete → deliver."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import discord
from discord.ext import commands

from .. import conversation, render
from ..bot import PsychographBot, is_allowed_channel
from ..personas import Persona
from .chess import board_file

log = logging.getLogger("psychograph.chat")


@dataclass(frozen=True)
class Request:
    messages: list[dict]
    source_urls: list[str]
    recipient: discord.User | None
    context_notice: str | None


async def show_generation_failure(placeholder: discord.Message, channel_id: int) -> None:
    try:
        await placeholder.edit(content="I couldn't reach the model. Check the bot and model server logs.")
    except discord.NotFound:
        log.info("Failed-response placeholder in channel %s was already deleted", channel_id)


class ChatCog(commands.Cog):
    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not await self._should_respond(message):
            return
        prompt = conversation.strip_mention(message.content, self.bot.user.id)
        if not prompt:
            await message.reply("Send me a message along with the mention.", mention_author=False)
            return

        persona = self.bot.personas.for_channel(message.channel.id, message.guild.id if message.guild else None)
        if persona.mode == "chess":
            await self._play_chess(message, prompt)
            return
        request = await self._build_request(message, prompt, persona)
        await self._respond(message, persona, request)

    async def _should_respond(self, message: discord.Message) -> bool:
        """In allowed channels, when mentioned or when someone replies to the bot."""
        bot_user = self.bot.user
        if message.author.bot or bot_user is None:
            return False
        if not is_allowed_channel(message.channel, self.bot.settings.allowed_channels):
            return False
        if bot_user in message.mentions:
            return True
        if message.reference is None or message.reference.message_id is None:
            return False
        referenced = message.reference.resolved
        if not isinstance(referenced, discord.Message):
            try:
                referenced = await message.channel.fetch_message(message.reference.message_id)
            except discord.HTTPException:
                return False
        return referenced.author.id == bot_user.id

    async def _play_chess(self, message: discord.Message, prompt: str) -> None:
        turn = await self.bot.chess.play(message.channel.id, prompt)
        file = board_file(turn.fen) if turn.fen else None
        reply = {"embed": render.board_embed("Chess", turn.summary, bool(file)), "mention_author": False}
        if file:
            reply["file"] = file
        await message.reply(**reply)

    async def _build_request(self, message: discord.Message, prompt: str, persona: Persona) -> Request:
        store, backend = self.bot.store, self.bot.backend
        async with message.channel.typing():
            tweets = await conversation.tweet_context(prompt, message.embeds)
        recipient = conversation.addressed_member(prompt, message.mentions, self.bot.user.id)
        user_turn = conversation.user_turn(prompt, tweets, recipient)

        parent_id = message.reference.message_id if message.reference else None
        history = store.message_chain(parent_id, message.channel.id) if parent_id else []
        store.save_message(message.id, parent_id, message.channel.id, "user", user_turn, message.author.id)

        verbosity = store.channel_settings(message.channel.id).verbosity
        messages, notice = conversation.fit_context(
            conversation.system_prompt(persona, verbosity),
            history,
            user_turn,
            backend.context_limit,
            backend.output_limit,
        )
        return Request(messages, [url for url, _text in tweets], recipient, notice)

    async def _respond(self, message: discord.Message, persona: Persona, request: Request) -> None:
        channel_id = message.channel.id
        placeholder = await message.reply("…", mention_author=False)
        try:
            started = time.perf_counter()
            completion = await self.bot.backend.complete(request.messages)
            summary = render.generation_summary(completion, time.perf_counter() - started)
            chunks, embeds = render.response_embeds(
                completion.text or "I don't have a response for that.",
                persona.name,
                request.source_urls,
                (summary, request.context_notice),
            )
            recipient = request.recipient
            sent = await placeholder.edit(
                content=recipient.mention if recipient else None,
                embed=embeds[0],
                allowed_mentions=discord.AllowedMentions(
                    users=[recipient] if recipient else [], roles=False, everyone=False, replied_user=False
                ),
            )
            if self.bot.store.channel_settings(channel_id).persona_reactions:
                try:
                    await sent.add_reaction(persona.reaction)
                except discord.HTTPException:
                    log.info("Couldn't add the persona reaction in channel %s", channel_id)

            parent_id = message.id
            for index, (chunk, embed) in enumerate(zip(chunks, embeds)):
                if index:
                    sent = await message.channel.send(
                        embed=embed, reference=sent, allowed_mentions=discord.AllowedMentions.none()
                    )
                self.bot.store.save_message(sent.id, parent_id, channel_id, "assistant", chunk)
                parent_id = sent.id
        except Exception:
            log.exception("Response failed in channel %s", channel_id)
            await show_generation_failure(placeholder, channel_id)


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(ChatCog(bot))
