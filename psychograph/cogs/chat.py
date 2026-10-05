"""Talking to the bot: mentions and replies, /ask, the "Ask persona" message command, and reaction controls.

React 🔁 on an answer to regenerate it, or 🗑️ to delete it (the asker, or anyone with Manage Messages).
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import conversation, render
from ..bot import PsychographBot, is_allowed_channel
from ..responder import Ask
from .chess import board_file

log = logging.getLogger("psychograph.chat")

REGENERATE, DELETE = "🔁", "🗑️"


def _choices(personas) -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=persona.name[:100], value=persona.key) for persona in personas][:25]


class ChatCog(commands.Cog):
    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot
        self.ask_menu = app_commands.ContextMenu(name="Ask persona", callback=self.ask_about_message)
        self.bot.tree.add_command(self.ask_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.ask_menu.name, type=self.ask_menu.type)

    # ── Messages ────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not await self._should_respond(message):
            return
        await self.handle(message)

    async def handle(self, message: discord.Message) -> None:
        prompt = conversation.strip_mention(message.content, self.bot.user.id)
        if not prompt:
            await message.reply("Send me a message along with the mention.", mention_author=False)
            return
        guild_id = message.guild.id if message.guild else None
        if self.bot.personas.for_channel(message.channel.id, guild_id).mode == "chess":
            await self._play_chess(message, prompt)
            return
        await self.bot.responder.answer(
            Ask(
                channel=message.channel,
                guild_id=guild_id,
                author=message.author,
                requester_id=message.author.id,
                text=prompt,
                record_id=message.id,
                parent_id=message.reference.message_id if message.reference else None,
                reply_to=message,
                embeds=message.embeds,
                mentions=message.mentions,
            )
        )

    async def _should_respond(self, message: discord.Message) -> bool:
        """In allowed channels, when mentioned or when someone replies to one of the bot's answers."""
        bot_user = self.bot.user
        if message.author.bot or message.webhook_id or bot_user is None:
            return False
        if not is_allowed_channel(message.channel, self.bot.settings.allowed_channels):
            return False
        if bot_user in message.mentions:
            return True
        if message.reference is None or message.reference.message_id is None:
            return False
        stored = self.bot.store.message(message.reference.message_id)
        if stored is not None:
            return stored["role"] == "assistant"  # includes persona-voice (webhook) answers
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

    # ── Reaction controls ───────────────────────────────────────────

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        emoji = str(payload.emoji)
        if emoji not in (REGENERATE, DELETE) or self.bot.user is None or payload.user_id == self.bot.user.id:
            return
        answer = self.bot.store.message(payload.message_id)
        if (
            answer is None
            or answer["role"] != "assistant"
            or answer["reply_to"] is None
            or answer["channel_id"] != payload.channel_id
        ):
            return
        request = self.bot.store.message(answer["reply_to"])
        # Whoever asked for the answer controls it — for "Ask persona" that's the person who ran the
        # command, not the author of the message it answered. Older rows predate requester_id.
        requester = answer["requester_id"] if answer["requester_id"] is not None else (request or {}).get("author_id")
        is_requester = requester == payload.user_id
        channel = self.bot.get_channel(payload.channel_id) or await self.bot.fetch_channel(payload.channel_id)
        if emoji == DELETE:
            member = payload.member
            can_moderate = bool(member and channel.permissions_for(member).manage_messages)
            if is_requester or can_moderate:
                await self.bot.responder.delete_response(channel, answer["reply_to"])
            return
        # Regenerating replays the request message, so only answers to the requester's own message qualify.
        if is_requester and request is not None and request["author_id"] == payload.user_id:
            await self._regenerate(channel, answer["reply_to"], payload.user_id)

    async def _regenerate(self, channel: discord.abc.Messageable, request_id: int, user_id: int) -> None:
        try:
            original = await channel.fetch_message(request_id)
        except discord.HTTPException:
            return  # /ask answers have no message to re-run; ask again instead
        if original.author.id != user_id:
            return
        await self.bot.responder.delete_response(channel, request_id)
        await self.handle(original)

    # ── /ask and the message command ────────────────────────────────

    @app_commands.command(name="ask", description="Get a one-off answer from any persona, without changing the channel's")
    @app_commands.describe(persona="Who should answer", prompt="What to ask")
    async def ask(self, interaction: discord.Interaction, persona: str, prompt: str) -> None:
        chosen = self.bot.personas.find(interaction.guild_id, persona)
        if chosen is None or chosen.mode != "chat":
            await interaction.response.send_message("Choose a chat persona from the list.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await self.bot.responder.answer(
            Ask(
                channel=interaction.channel,
                guild_id=interaction.guild_id,
                author=interaction.user,
                requester_id=interaction.user.id,
                text=prompt,
                record_id=interaction.id,
                persona=chosen,
                followup=interaction.followup,
            )
        )

    @ask.autocomplete("persona")
    async def ask_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return _choices(p for p in self.bot.personas.search(interaction.guild_id, current) if p.mode == "chat")

    async def ask_about_message(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """Right-click a message → Apps → Ask persona: the channel's persona responds to it."""
        if message.author.bot or message.webhook_id:
            await interaction.response.send_message("That's a bot message — reply to it to keep talking.", ephemeral=True)
            return
        text = message.content.strip() or " ".join(filter(None, (embed.description for embed in message.embeds)))
        persona = self.bot.personas.for_channel(interaction.channel_id, interaction.guild_id)
        if not text or persona.mode != "chat":
            reason = "That message has no text to respond to." if not text else "Switch to a chat persona first."
            await interaction.response.send_message(reason, ephemeral=True)
            return
        await interaction.response.send_message(f"{persona.reaction} Asking **{persona.name}**…", ephemeral=True, delete_after=5)
        await self.bot.responder.answer(
            Ask(
                channel=interaction.channel,
                guild_id=interaction.guild_id,
                author=message.author,
                requester_id=interaction.user.id,
                text=text,
                record_id=message.id,
                parent_id=message.reference.message_id if message.reference else None,
                reply_to=message,
                embeds=message.embeds,
                mentions=message.mentions,
            )
        )


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(ChatCog(bot))
