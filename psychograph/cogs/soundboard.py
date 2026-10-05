"""/sound: play a soundbank clip by hand, or list them all."""

from __future__ import annotations

from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands

from .. import sounds
from ..bot import PsychographBot
from ..render import EMBED_COLOR


def soundboard_embed(bank: sounds.Soundbank) -> discord.Embed:
    embed = discord.Embed(title="🔊 Soundboard", color=EMBED_COLOR)
    if not len(bank):
        embed.description = "No sounds are built yet. Run `python tools/build_soundbank.py` on the bot's machine."
        return embed
    by_mood: dict[str, list[str]] = defaultdict(list)
    for sound in bank.sounds.values():
        by_mood[sound.mood or "misc"].append(f"`{sound.key}`")
    for mood, names in sorted(by_mood.items()):
        embed.add_field(name=mood.title(), value=" ".join(names)[:1024], inline=False)
    embed.set_footer(text="/sound <name> to play · turn on Sounds in /status to let personas use them")
    return embed


class SoundboardCommands(commands.Cog):
    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot

    @app_commands.command(name="sound", description="Play a sound from the soundboard, or list them")
    @app_commands.describe(name="Leave blank to see every sound")
    @app_commands.checks.cooldown(1, 8.0, key=lambda interaction: (interaction.guild_id, interaction.user.id))
    async def sound(self, interaction: discord.Interaction, name: str | None = None) -> None:
        bank = self.bot.soundbank
        if name is None:
            await interaction.response.send_message(embed=soundboard_embed(bank), ephemeral=True)
            return
        sound = bank.get(name)
        if sound is None:
            await interaction.response.send_message("No sound by that name — try `/sound` for the list.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"-# 🔊 {interaction.user.mention} played **{sound.label}**",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await sounds.play(interaction.channel, sound, reference=await interaction.original_response())

    @sound.autocomplete("name")
    async def sound_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [
            app_commands.Choice(name=f"{sound.label} · {sound.description}"[:100], value=sound.key)
            for sound in self.bot.soundbank.search(current)
        ][:25]

    @sound.error
    async def sound_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            await interaction.response.send_message(
                f"Easy — try again in {error.retry_after:.0f}s.", ephemeral=True
            )
            return
        raise error


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(SoundboardCommands(bot))
