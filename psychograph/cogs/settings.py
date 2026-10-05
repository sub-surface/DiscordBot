"""Per-channel settings: /status (with controls), /verbosity, /reactions, /reset."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import PsychographBot
from ..render import EMBED_COLOR
from ..store import VERBOSITY_LEVELS


def status_embed(bot: PsychographBot, channel_id: int, guild_id: int | None, channel_name: str) -> discord.Embed:
    settings = bot.store.channel_settings(channel_id)
    persona = bot.personas.for_channel(channel_id, guild_id)
    backend = bot.backend
    embed = discord.Embed(
        title=f"#{channel_name} settings",
        description="Channel-specific chat settings. Conversation history follows reply chains, not the whole channel.",
        color=EMBED_COLOR,
    )
    embed.set_thumbnail(url=persona.avatar_url)
    embed.add_field(name="Persona", value=f"{persona.reaction} {persona.name}", inline=True)
    embed.add_field(name="Reply detail", value=settings.verbosity.title(), inline=True)
    embed.add_field(name="Persona reactions", value="On" if settings.persona_reactions else "Off", inline=True)
    embed.add_field(name="Voice", value="Speaks as persona" if settings.persona_voice else "Bot embeds", inline=True)
    embed.add_field(
        name="Sounds", value=f"On · {len(bot.soundbank)} clips" if settings.sounds else "Off", inline=True
    )
    embed.add_field(name="Backend", value=backend.name.title(), inline=True)
    embed.add_field(
        name="Context / output", value=f"{backend.context_limit:,} / {backend.output_limit:,} tokens", inline=True
    )
    embed.add_field(name="Model profile", value=backend.profile.describe(), inline=False)
    embed.add_field(name="Model target", value=f"`{backend.label}`", inline=False)
    if persona.mode == "chess":
        embed.add_field(name="Chess commentary", value="On" if settings.chess_commentary else "Off", inline=True)
    embed.set_footer(text="History is isolated per channel/thread. Use Reset history to clear this channel.")
    return embed


class ResetHistoryView(discord.ui.View):
    def __init__(self, bot: PsychographBot, channel_id: int, channel_name: str, requester_id: int) -> None:
        super().__init__(timeout=60)
        self.bot = bot
        self.channel_id = channel_id
        self.channel_name = channel_name
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message("Only the person who opened this confirmation can use it.", ephemeral=True)
        return False

    @discord.ui.button(label="Clear history", style=discord.ButtonStyle.danger)
    async def confirm_reset(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.stop()
        if interaction.channel_id != self.channel_id:
            await interaction.response.edit_message(content="This confirmation belongs to another channel.", view=None)
            return
        self.bot.store.clear_channel(self.channel_id)
        await interaction.response.edit_message(
            content=f"Conversation history cleared for **#{self.channel_name}**.", view=None
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_reset(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="History reset cancelled.", view=None)


def reset_prompt(bot: PsychographBot, interaction: discord.Interaction) -> dict:
    channel_name = getattr(interaction.channel, "name", "channel")
    return {
        "content": f"Clear conversation history for **#{channel_name}**? This cannot be undone.",
        "view": ResetHistoryView(bot, interaction.channel_id, channel_name, interaction.user.id),
        "ephemeral": True,
    }


class StatusView(discord.ui.View):
    def __init__(
        self,
        bot: PsychographBot,
        channel_id: int,
        guild_id: int | None,
        channel_name: str,
        requester_id: int,
        can_manage_messages: bool,
    ) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.channel_id = channel_id
        self.guild_id = guild_id
        self.channel_name = channel_name
        self.requester_id = requester_id
        self.persona_select = discord.ui.Select(placeholder="Choose a persona", options=self._persona_options(), row=0)
        self.persona_select.callback = self.select_persona
        self.add_item(self.persona_select)
        if not can_manage_messages:
            self.remove_item(self.reaction_button)
            self.remove_item(self.voice_button)
            self.remove_item(self.sounds_button)
        self._refresh_labels()

    def _persona_options(self) -> list[discord.SelectOption]:
        current = self.bot.personas.for_channel(self.channel_id, self.guild_id).key
        personas = self.bot.personas.available(self.guild_id)
        personas.sort(key=lambda persona: persona.key != current)  # current first, so it survives the cap
        return [
            discord.SelectOption(label=persona.name[:100], value=persona.key, default=persona.key == current)
            for persona in personas[:25]
        ]

    def _refresh_labels(self) -> None:
        settings = self.bot.store.channel_settings(self.channel_id)
        self.verbosity_button.label = f"Detail: {settings.verbosity.title()}"
        self.reaction_button.label = f"Reactions: {'On' if settings.persona_reactions else 'Off'}"
        self.voice_button.label = f"Voice: {'Persona' if settings.persona_voice else 'Embed'}"
        self.sounds_button.label = f"Sounds: {'On' if settings.sounds else 'Off'}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message("Run `/status` to open controls for yourself.", ephemeral=True)
            return False
        if interaction.channel_id != self.channel_id:
            await interaction.response.send_message("These controls belong to another channel.", ephemeral=True)
            return False
        return True

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self.persona_select.options = self._persona_options()
        self._refresh_labels()
        await interaction.response.edit_message(
            embed=status_embed(self.bot, self.channel_id, self.guild_id, self.channel_name), view=self
        )

    async def select_persona(self, interaction: discord.Interaction) -> None:
        persona = self.bot.personas.get(self.guild_id, self.persona_select.values[0])
        if persona is None:
            await interaction.response.send_message("That persona isn't available in this server.", ephemeral=True)
            return
        self.bot.store.update_channel(self.channel_id, persona=persona.key)
        await self._refresh(interaction)

    @discord.ui.button(label="Detail", style=discord.ButtonStyle.secondary, row=1)
    async def verbosity_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        current = self.bot.store.channel_settings(self.channel_id).verbosity
        index = VERBOSITY_LEVELS.index(current) if current in VERBOSITY_LEVELS else -1
        next_level = VERBOSITY_LEVELS[(index + 1) % len(VERBOSITY_LEVELS)]
        self.bot.store.update_channel(self.channel_id, verbosity=next_level)
        await self._refresh(interaction)

    @discord.ui.button(label="Reactions", style=discord.ButtonStyle.secondary, row=1)
    async def reaction_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not interaction.permissions.manage_messages:
            await interaction.response.send_message("Manage Messages permission is required.", ephemeral=True)
            return
        enabled = self.bot.store.channel_settings(self.channel_id).persona_reactions
        self.bot.store.update_channel(self.channel_id, persona_reactions=not enabled)
        await self._refresh(interaction)

    @discord.ui.button(label="Voice", style=discord.ButtonStyle.secondary, row=1)
    async def voice_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not interaction.permissions.manage_messages:
            await interaction.response.send_message("Manage Messages permission is required.", ephemeral=True)
            return
        enabled = self.bot.store.channel_settings(self.channel_id).persona_voice
        if not enabled and not self.bot.webhooks.available(interaction.channel):
            await interaction.response.send_message(
                "To speak as personas I need the **Manage Webhooks** permission in this channel.", ephemeral=True
            )
            return
        self.bot.store.update_channel(self.channel_id, persona_voice=not enabled)
        await self._refresh(interaction)

    @discord.ui.button(label="Sounds", style=discord.ButtonStyle.secondary, row=2)
    async def sounds_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not interaction.permissions.manage_messages:
            await interaction.response.send_message("Manage Messages permission is required.", ephemeral=True)
            return
        enabled = self.bot.store.channel_settings(self.channel_id).sounds
        if not enabled and not len(self.bot.soundbank):
            await interaction.response.send_message(
                "No sounds are built yet — run `python tools/build_soundbank.py` on the bot's machine.", ephemeral=True
            )
            return
        self.bot.store.update_channel(self.channel_id, sounds=not enabled)
        await self._refresh(interaction)

    @discord.ui.button(label="Reset history", style=discord.ButtonStyle.danger, row=1)
    async def reset_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_message(**reset_prompt(self.bot, interaction))


class SettingsCommands(commands.Cog):
    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot

    @app_commands.command(name="status", description="Show this channel's settings and bot backend")
    async def status(self, interaction: discord.Interaction) -> None:
        channel_name = getattr(interaction.channel, "name", "channel")
        view = StatusView(
            self.bot,
            interaction.channel_id,
            interaction.guild_id,
            channel_name,
            interaction.user.id,
            bool(interaction.permissions and interaction.permissions.manage_messages),
        )
        await interaction.response.send_message(
            embed=status_embed(self.bot, interaction.channel_id, interaction.guild_id, channel_name),
            view=view,
            ephemeral=True,
        )

    @app_commands.command(name="verbosity", description="Show or set reply detail for this channel")
    @app_commands.describe(level="How detailed future replies should be")
    @app_commands.choices(level=[app_commands.Choice(name=level.title(), value=level) for level in VERBOSITY_LEVELS])
    async def verbosity(self, interaction: discord.Interaction, level: str | None = None) -> None:
        if level is None:
            current = self.bot.store.channel_settings(interaction.channel_id).verbosity
            await interaction.response.send_message(
                f"Reply detail is **{current}**. Choose concise, balanced, or detailed to change it.", ephemeral=True
            )
            return
        self.bot.store.update_channel(interaction.channel_id, verbosity=level)
        await interaction.response.send_message(f"Future replies in this channel will be **{level}**.", ephemeral=True)

    @app_commands.command(name="reactions", description="Toggle persona signature reactions in this channel")
    @app_commands.default_permissions(manage_messages=True)
    @app_commands.describe(enabled="Whether the bot should add a persona reaction after replies")
    @app_commands.choices(enabled=[app_commands.Choice(name="On", value="on"), app_commands.Choice(name="Off", value="off")])
    async def reactions(self, interaction: discord.Interaction, enabled: str | None = None) -> None:
        if interaction.guild is None:
            await interaction.response.send_message("This setting is only available in a server.", ephemeral=True)
            return
        if not interaction.permissions.manage_messages:
            await interaction.response.send_message("You need Manage Messages to change channel reactions.", ephemeral=True)
            return
        if enabled is None:
            state = "on" if self.bot.store.channel_settings(interaction.channel_id).persona_reactions else "off"
            await interaction.response.send_message(f"Persona reactions are **{state}** in this channel.", ephemeral=True)
            return
        self.bot.store.update_channel(interaction.channel_id, persona_reactions=enabled == "on")
        await interaction.response.send_message(f"Persona reactions are now **{enabled}** in this channel.", ephemeral=True)

    @app_commands.command(name="reset", description="Clear this channel's conversation history")
    async def reset(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(**reset_prompt(self.bot, interaction))


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(SettingsCommands(bot))
