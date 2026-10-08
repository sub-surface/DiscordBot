"""Talking to the bot: mentions and replies, /ask, the message commands, and reaction controls.

A message reaches the bot by @mention, by replying to one of its answers, or by naming a persona
("aura-bot thoughts?" for a chatter, "mochi, thoughts?" for a character, see ambient.py); anything else may
get a server-emote reaction from the channel's persona. Every message to the bot goes through one System 1 triage (a Jev call, see Responder.triage) that decides where
it goes and what context it needs:

  quick     a /quick feature (decide, odds, tier list…): answered by Jev alone, no model
  direct    a question about the bot itself: answered from code
  tool      a tool persona (judge, minutes…): Jev screens, the model writes
  chat      the channel's persona: the model, shown only the channel messages that matter

React 🔁 on an answer to regenerate it, or 🗑️ to delete it (the asker, or anyone with Manage Messages).
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import conversation, quick, render
from ..bot import PsychographBot, is_allowed_channel
from ..jev import DIRECT
from ..personas import Persona
from ..quick import QUICKS, Quick, QuickRequest
from ..render import persona_choices
from ..repeats import RepeatGuard
from ..responder import Ask, react, unreact
from .chess import board_file
from .help import help_embed, model_embed, personas_embed

log = logging.getLogger("psychograph.chat")

REGENERATE, DELETE = "🔁", "🗑️"


def chatters(bot: PsychographBot, guild_id: int | None) -> list[Persona]:
    return [persona for persona in bot.personas.available(guild_id) if persona.group == "chatters"]


def remember_quick(
    bot: PsychographBot, feature: Quick, request: QuickRequest, embed: discord.Embed, sent: discord.Message | None,
    record_id: int, channel_id: int, parent_id: int | None = None,
) -> None:
    """Keep a quick answer in the reply chain, as what Jev was shown and said, so a reply asking "why 81%?"
    reaches the channel's persona with the numbers in front of it."""
    if sent is None or not getattr(sent, "id", None):
        return
    asked = (request.target or request.text).strip() or feature.key
    store = bot.store
    store.save_message(record_id, parent_id, channel_id, "user", f"{request.speaker}: {asked}", request.author_id)
    store.save_message(
        sent.id, record_id, channel_id, "assistant", quick.record(feature, request, embed),
        reply_to=record_id, requester_id=request.author_id, answer_id=sent.id, persona=f"quick:{feature.key}",
    )


class ChatCog(commands.Cog):
    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot
        self.repeats = RepeatGuard()
        self.menus = [
            app_commands.ContextMenu(name="Ask persona", callback=self.ask_about_message),
            app_commands.ContextMenu(name="Review this debate", callback=self.review_debate),
            app_commands.ContextMenu(name="Judge this", callback=self.judge_message),
        ]
        for menu in self.menus:
            self.bot.tree.add_command(menu)

    async def cog_unload(self) -> None:
        for menu in self.menus:
            self.bot.tree.remove_command(menu.name, type=menu.type)

    # ── Messages ────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not is_allowed_channel(message.channel, self.bot.settings.allowed_channels):
            return
        previous = await self.bot.ambient.seen(message)  # every message, the bot's too: how long the channel was quiet
        if not self._listening(message):
            return
        if await self._addressed(message):
            await self.handle(message)
        elif (persona := await self.bot.ambient.called(message)) is not None:
            await self.handle(message, called=persona)
        else:
            await self.bot.ambient.react(message, previous)

    async def handle(self, message: discord.Message, regenerating: bool = False, called: Persona | None = None) -> None:
        """Answer a message sent to the bot; `called` is the persona it named instead of mentioning the bot."""
        prompt = conversation.strip_mention(message.content, self.bot.user.id)
        if not prompt:
            await message.reply("Send me a message along with the mention.", mention_author=False)
            return
        guild_id = message.guild.id if message.guild else None
        responder, me = self.bot.responder, self.bot.user
        # React before triage (a Jev call), so the asker sees at once that the message landed.
        status = responder.status(message.guild)
        await react(message, status)
        ask = Ask(
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
            shown_status=status,
        )
        route, target = await self._route(message, ask, prompt, called)
        if route != "chat":
            await unreact(message, status, me)
            if route == "quick":
                await self._answer_quick(message, ask, *target)
            elif route == "direct":
                await self._answer_directly(message, target, guild_id)
            else:
                await self._play_chess(message, prompt)
            return
        ask.persona = target
        # Only plain persona chat (a model call) is checked for repeats: commands, tools and instant answers
        # can be asked again freely, since they're cheap or read a channel that has moved on.
        chat = ask.persona is None or ask.persona.group != "tools"
        earlier = self.repeats.earlier(message.channel.id, prompt) if chat and not regenerating else None
        if earlier is not None:
            await unreact(message, status, me)
            await message.reply(f"-# ↩ {earlier.link}", mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            return
        sent = await responder.answer(ask)
        if sent and chat:
            self.repeats.record(message.channel.id, prompt, getattr(sent[0], "jump_url", ""))

    async def _route(
        self, message: discord.Message, ask: Ask, prompt: str, called: Persona | None
    ) -> tuple[str, object]:
        """Where a message goes, first match wins: a quick feature or tool called by name, the persona it names
        or replies to, chess moves, then System 1's triage of a fresh message (a quick feature, a question
        about the bot, a tool), else the channel's persona. Returns ("quick", (feature, text)), ("direct",
        key), ("chess", None) or ("chat", persona, None meaning the channel's)."""
        registry, guild_id = self.bot.personas, ask.guild_id
        quick_call = None if called else quick.quick_for(prompt)
        if quick_call is not None:
            return "quick", quick_call
        persona, fresh = (called, False) if called else self._named_or_continued(message, prompt, guild_id)
        channel = registry.for_channel(message.channel.id, guild_id)
        if persona is None and channel.mode == "chess":
            return "chess", None
        if persona is not None and persona.group == "tools":
            return "chat", persona
        # Triage runs even without routes: it also picks which channel messages a chat persona sees.
        kind = await self.bot.responder.triage(ask, self._routes(channel) if fresh else {})
        if kind in QUICKS:
            return "quick", (QUICKS[kind], prompt)
        if kind in DIRECT:
            return "direct", kind
        return "chat", registry.get(guild_id, kind) if kind else persona

    def _named_or_continued(self, message: discord.Message, prompt: str, guild_id: int | None) -> tuple[Persona | None, bool]:
        """A tool called by name ("judge: …"), or the persona whose answer this replies to, and whether the
        message is fresh (so triage may route it). A reply to a tool's answer goes back to the channel's
        persona: a tool does one job, and the channel's persona sees the tool's answer quoted."""
        tool = self.bot.personas.tool_for(prompt)
        if tool is not None:
            return tool, False
        if message.reference is not None and message.reference.message_id is not None:
            stored = self.bot.store.message(message.reference.message_id)
            if stored is not None and stored["role"] == "assistant":
                answered = self.bot.personas.get(guild_id, stored["persona"]) if stored["persona"] else None
                return (None if answered is None or answered.group == "tools" else answered), False
        return None, True

    def _routes(self, channel: Persona) -> dict[str, str]:
        """Where triage may send a fresh mention: quick features and questions about the bot on any model,
        tools only on models that run them (and not from a channel that is already a tool)."""
        routes = {**DIRECT, **quick.routes()}
        if self.bot.backend.profile.tools and channel.group != "tools":
            routes.update({tool.key: tool.intent for tool in self.bot.personas.tools() if tool.intent})
        return routes

    async def _answer_quick(self, message: discord.Message, ask: Ask, feature: Quick, text: str) -> None:
        referenced = message.reference.resolved if message.reference else None
        target = ""
        if isinstance(referenced, discord.Message) and not referenced.author.bot:
            target = referenced.clean_content
        request = QuickRequest(
            text=text,
            speaker=getattr(message.author, "display_name", None) or message.author.name,
            author_id=message.author.id,
            said=await self.bot.responder.recent(ask),
            target=target,
            chatters=chatters(self.bot, ask.guild_id),
        )
        embed = await feature.run(self.bot.jev, request)
        sent = await message.reply(embed=embed, mention_author=False, allowed_mentions=discord.AllowedMentions.none())
        remember_quick(
            self.bot, feature, request, embed, sent, record_id=message.id, channel_id=message.channel.id,
            parent_id=message.reference.message_id if message.reference else None,
        )

    async def _answer_directly(self, message: discord.Message, key: str, guild_id: int | None) -> None:
        """Questions about the bot itself, answered from code: no model call, no GPU."""
        if key == "bot_help":
            embed = help_embed(self.bot, message.channel.id, guild_id)
        elif key == "bot_persona":
            embed = personas_embed(self.bot, message.channel.id, guild_id)
        else:
            embed = model_embed(self.bot)
        embed.set_footer(text=quick.INSTANT_FOOTER)
        await message.reply(embed=embed, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

    def _listening(self, message: discord.Message) -> bool:
        """A member's message in an allowed channel, from someone not timed out (who is ignored silently)."""
        if message.author.bot or message.webhook_id or self.bot.user is None:
            return False
        if not is_allowed_channel(message.channel, self.bot.settings.allowed_channels):
            return False
        return self.bot.ignoring(message.guild.id if message.guild else None, message.author.id) is None

    async def _addressed(self, message: discord.Message) -> bool:
        """Mentioned, or a reply to one of the bot's answers."""
        bot_user = self.bot.user
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
        if self.bot.ignoring(getattr(payload, "guild_id", None), payload.user_id) is not None:
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
                await self.bot.responder.delete_answer(channel, payload.message_id)
            return
        # Regenerating replays the request message, so only answers to the requester's own message qualify.
        if is_requester and request is not None and request["author_id"] == payload.user_id:
            await self._regenerate(channel, answer["reply_to"], payload.message_id, payload.user_id, answer["persona"])

    async def _regenerate(
        self, channel: discord.abc.Messageable, request_id: int, answer_message_id: int, user_id: int,
        persona_key: str | None = None,
    ) -> None:
        try:
            original = await channel.fetch_message(request_id)
        except discord.HTTPException:
            return  # /ask answers have no message to re-run; ask again instead
        if original.author.id != user_id:
            return
        await self.bot.responder.delete_answer(channel, answer_message_id)  # this answer only, not others'
        # A message that named a persona instead of mentioning the bot goes back to that persona.
        named = persona_key and self.bot.user not in original.mentions and original.reference is None
        guild_id = original.guild.id if original.guild else None
        await self.handle(original, regenerating=True, called=self.bot.personas.get(guild_id, persona_key) if named else None)

    # ── /ask and the message command ────────────────────────────────

    @app_commands.command(name="ask", description="Get a one-off answer from any persona, without changing the channel's")
    @app_commands.describe(persona="Who should answer", prompt="What to ask")
    async def ask(self, interaction: discord.Interaction, persona: str, prompt: str) -> None:
        chosen = self.bot.personas.find(interaction.guild_id, persona)
        if chosen is None or chosen.mode != "chat":
            await interaction.response.send_message("Choose a chat persona from the list.", ephemeral=True)
            return
        if not self.bot.responder.can_run(chosen):
            await interaction.response.send_message(self.bot.responder.tools_unavailable(chosen), ephemeral=True)
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
        return persona_choices([p for p in self.bot.personas.search(interaction.guild_id, current) if p.mode == "chat"])

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
                # No pings: these are someone else's words, so a "tell @x" in them mustn't ping x.
                mentions=(),
            )
        )

    async def review_debate(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """Right-click → Apps → Review this debate: the review reads the channel up to and including this message."""
        await self._tool_on_message(interaction, message, "debate_review", "Review the debate that ends with this message.")

    async def judge_message(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """Right-click → Apps → Judge this: the judge rules on this message, with what came before as context."""
        who = getattr(message.author, "display_name", message.author.name)
        text = message.clean_content.strip() if isinstance(message.clean_content, str) else ""
        if not text:
            await interaction.response.send_message("That message has no text to judge.", ephemeral=True)
            return
        await self._tool_on_message(interaction, message, "judge", f'Judge what {who} said: "{text}"')

    async def _tool_on_message(self, interaction: discord.Interaction, message: discord.Message, key: str, text: str) -> None:
        tool = self.bot.personas.get(interaction.guild_id, key)
        if tool is None:
            await interaction.response.send_message("That tool isn't installed.", ephemeral=True)
            return
        if not self.bot.responder.can_run(tool):
            await interaction.response.send_message(self.bot.responder.tools_unavailable(tool), ephemeral=True)
            return
        await interaction.response.send_message(f"{tool.reaction} Asking **{tool.name}**…", ephemeral=True, delete_after=5)
        await self.bot.responder.answer(
            Ask(
                channel=interaction.channel,
                guild_id=interaction.guild_id,
                author=interaction.user,
                requester_id=interaction.user.id,
                text=text,
                record_id=interaction.id,
                reply_to=message,
                persona=tool,
                include_reply_to=True,
            )
        )


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(ChatCog(bot))
