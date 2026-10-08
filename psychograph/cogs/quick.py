"""/quick: instant answers from Jev alone (decide, odds, tier list, rate, tone, vibe, which chatter),
plus the "Tone check" message command. No language model, no GPU."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from .. import quick as answers
from ..bot import PsychographBot
from ..responder import recent_messages
from .chat import chatters, remember_quick


class QuickCommands(commands.Cog):
    quick = app_commands.Group(name="quick", description="Instant answers, no model: decide, odds, tier lists and more")

    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot
        self.tone_menu = app_commands.ContextMenu(name="Tone check", callback=self.tone_check)
        self.bot.tree.add_command(self.tone_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.tone_menu.name, type=self.tone_menu.type)

    async def _reply(
        self, interaction: discord.Interaction, key: str, text: str = "", target: str = "",
        member: discord.abc.User | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True)
        who = member or interaction.user
        request = answers.QuickRequest(
            text=text,
            speaker=getattr(who, "display_name", None) or who.name,
            author_id=who.id,
            said=await recent_messages(interaction.channel),
            target=target,
            chatters=chatters(self.bot, interaction.guild_id),
        )
        feature = answers.QUICKS[key]
        embed = await feature.run(self.bot.jev, request)
        sent = await interaction.followup.send(embed=embed, allowed_mentions=discord.AllowedMentions.none(), wait=True)
        remember_quick(self.bot, feature, request, embed, sent, record_id=interaction.id, channel_id=interaction.channel_id)

    @quick.command(name="decide", description="Pick between options, with the odds for each")
    @app_commands.describe(options="Two or more options: pizza or curry or a shake")
    async def decide(self, interaction: discord.Interaction, options: str) -> None:
        await self._reply(interaction, "decide", options)

    @quick.command(name="odds", description="How likely is it? A probability for a yes/no question")
    @app_commands.describe(question="A yes/no question: will zack mention his shin today")
    async def odds(self, interaction: discord.Interaction, question: str) -> None:
        await self._reply(interaction, "odds", question)

    @quick.command(name="tier", description="Put a list of things into tiers, S to F")
    @app_commands.describe(items="Comma-separated: crocs, tea, jury duty")
    async def tier(self, interaction: discord.Interaction, items: str) -> None:
        await self._reply(interaction, "tier", items)

    @quick.command(name="rate", description="Rate a take: how divisive, how defensible, sincere or a bit")
    @app_commands.describe(take="The take: cereal is a soup")
    async def rate(self, interaction: discord.Interaction, take: str) -> None:
        await self._reply(interaction, "rate", take)

    @quick.command(name="tone", description="Is it sarcastic, joking, sincere…?")
    @app_commands.describe(text="The message to read")
    async def tone(self, interaction: discord.Interaction, text: str) -> None:
        await self._reply(interaction, "tone", target=text)

    @quick.command(name="vibe", description="A vibe check of the last 40 messages")
    async def vibe(self, interaction: discord.Interaction) -> None:
        await self._reply(interaction, "vibe")

    @quick.command(name="chatter", description="Which chatter persona does someone sound most like?")
    @app_commands.describe(member="Who to read (default: you)")
    async def chatter(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        await self._reply(interaction, "chatter", member=member)

    async def tone_check(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """Right-click a message → Apps → Tone check."""
        if not message.clean_content.strip():
            await interaction.response.send_message("That message has no text to read.", ephemeral=True)
            return
        await self._reply(interaction, "tone", target=message.clean_content)


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(QuickCommands(bot))
