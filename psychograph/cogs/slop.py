"""/santi-slop: the Slop Report for #sim-city, posted for everyone. See slop.py."""

from __future__ import annotations

import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from .. import slop
from ..bot import PsychographBot

log = logging.getLogger("psychograph.slop")


class SlopCommands(commands.Cog):
    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot
        self._lock = asyncio.Lock()  # one scan at a time

    @app_commands.command(name="santi-slop", description="The Slop Report: the most viral posts shared in #sim-city")
    @app_commands.describe(period="How far back to look")
    @app_commands.choices(period=[app_commands.Choice(name=name.title(), value=name) for name in slop.PERIODS])
    @app_commands.guild_only()
    async def santi_slop(self, interaction: discord.Interaction, period: str = "week") -> None:
        name = self.bot.settings.slop_channel.casefold()
        channel = next((c for c in interaction.guild.text_channels if c.name.casefold() == name), None)
        if channel is None:
            await interaction.response.send_message(f"There's no #{self.bot.settings.slop_channel} here.", ephemeral=True)
            return
        if self.bot.ignoring(interaction.guild_id, interaction.user.id):
            await interaction.response.send_message("The bot is ignoring you for now.", ephemeral=True)
            return
        if not channel.permissions_for(interaction.user).read_message_history:
            # the report reposts the channel's contents, so only for those who can read it
            await interaction.response.send_message(f"You can't read #{channel.name}.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            async with self._lock:
                embed, card = await slop.report(self.bot, channel, period)
        except Exception:
            log.exception("Slop report failed")
            await interaction.followup.send("The slop cannon jammed. Try again in a moment.", ephemeral=True)
            return
        await interaction.followup.send(embed=embed, file=card, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(SlopCommands(bot))
