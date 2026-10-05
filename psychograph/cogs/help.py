"""/help: a short tour of everything the bot can do."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import PsychographBot
from ..render import EMBED_COLOR


def help_embed(bot: PsychographBot, channel_id: int, guild_id: int | None) -> discord.Embed:
    persona = bot.personas.for_channel(channel_id, guild_id)
    backend = bot.backend
    embed = discord.Embed(
        title="Psychograph",
        description=(
            f"{persona.reaction} This channel is talking to **{persona.name}**. "
            "Mention me or reply to one of my messages to chat — replies keep the thread's memory."
        ),
        color=EMBED_COLOR,
    )
    embed.set_thumbnail(url=persona.avatar_url)
    embed.add_field(
        name="💬 Talking",
        value=(
            "**@mention** or **reply** to chat · `tell @someone a poem` pings just them\n"
            "**/ask** a one-off answer from any persona\n"
            "Right-click a message → **Apps → Ask persona** to have the persona respond to it\n"
            "Paste an x.com link and I'll read the post"
        ),
        inline=False,
    )
    embed.add_field(
        name="🎛️ On my answers",
        value="React 🔁 to regenerate (asker) · 🗑️ to delete (asker or moderators)\n"
        "While I work: 👀 thinking · ☁️ waking the GPU · ⏳ queued",
        inline=False,
    )
    embed.add_field(
        name="🎭 Personas",
        value="**/persona** switch · **/persona-create** make your own · **/persona-edit** · **/persona-delete**\n"
        "**/status** → *Voice* to speak as the persona with its own name and avatar",
        inline=False,
    )
    embed.add_field(
        name="⚙️ Channel",
        value="**/status** settings & controls · **/verbosity** reply length · **/reactions** signature emoji · "
        "**/reset** clear memory",
        inline=False,
    )
    embed.add_field(name="♟️ Chess", value="**/chess new** · **/chess move** · then just mention me with moves", inline=False)
    embed.set_footer(text=f"{backend.name.title()} · {backend.profile.describe()} · /model · /stats")
    return embed


class HelpCommands(commands.Cog):
    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="What I can do and how to use it")
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=help_embed(self.bot, interaction.channel_id, interaction.guild_id), ephemeral=True
        )


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(HelpCommands(bot))
