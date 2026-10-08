"""Per-channel settings: /status (with controls for everything), /verbosity, /reset, and /timeout."""

from __future__ import annotations

import re
import time

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import PsychographBot
from ..render import CHOICE_LIMIT, EMBED_COLOR
from ..store import VERBOSITY_LEVELS


MAX_TIMEOUT_SECONDS = 7 * 24 * 3600
PERSONA_MENUS = (  # one dropdown each, since Discord caps a dropdown at 25
    ("Chatters", lambda persona: persona.group == "chatters"),
    ("Characters, tools and custom", lambda persona: persona.group != "chatters"),
)
_DURATION = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(s|sec|secs|m|min|mins|h|hr|hrs|hours?|d|days?)?\s*$", re.IGNORECASE)
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int | None:
    """ "30m" → 1800. A bare number is minutes. None if unreadable, zero, or over a week."""
    match = _DURATION.match(text)
    if not match:
        return None
    unit = (match.group(2) or "m")[0].casefold()
    seconds = int(float(match.group(1)) * _UNITS[unit])
    return seconds if 0 < seconds <= MAX_TIMEOUT_SECONDS else None


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
    embed.add_field(name="Reactions", value="Server emotes" if settings.reactions else "Off", inline=True)
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
        # One dropdown per menu (Discord caps each at 25): chatters, then everyone else.
        self.persona_selects: list[discord.ui.Select] = []
        for row, (placeholder, _belongs) in enumerate(PERSONA_MENUS):
            select = discord.ui.Select(placeholder=placeholder, options=[discord.SelectOption(label="…")], row=row)
            select.callback = self.select_persona
            self.persona_selects.append(select)
        self._fill_persona_selects()
        if not can_manage_messages:
            self.remove_item(self.reaction_button)
            self.remove_item(self.voice_button)
            self.remove_item(self.sounds_button)
        self._refresh_labels()

    def _fill_persona_selects(self) -> None:
        """Each menu's personas in group order; a menu with none is left out."""
        available = self.bot.personas.available(self.guild_id)
        current = self.bot.personas.for_channel(self.channel_id, self.guild_id)
        for select, (_placeholder, belongs) in zip(self.persona_selects, PERSONA_MENUS):
            personas = [persona for persona in available if belongs(persona)]
            options = self._persona_options(personas, current)
            if options:
                select.options = options
                if select not in self.children:
                    self.add_item(select)
            elif select in self.children:
                self.remove_item(select)

    def _persona_options(self, personas: list, current) -> list[discord.SelectOption]:
        """In group order; past the cap, the current persona takes the last slot so it stays selected."""
        if not personas:
            return []
        in_menu = any(persona.key == current.key for persona in personas)
        personas = personas[:CHOICE_LIMIT]
        if in_menu and current.key not in {persona.key for persona in personas}:
            personas[-1] = current
        return [
            discord.SelectOption(
                label=persona.label[:100],
                value=persona.key,
                description=(
                    persona.about if self.bot.responder.can_run(persona) else "Needs a tools model (MiMo)"
                )[:100] or persona.group.title(),
                default=persona.key == current.key,
            )
            for persona in personas
        ]

    def _refresh_labels(self) -> None:
        settings = self.bot.store.channel_settings(self.channel_id)
        self.verbosity_button.label = f"Detail: {settings.verbosity.title()}"
        self.reaction_button.label = f"Reactions: {'On' if settings.reactions else 'Off'}"
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
        self._fill_persona_selects()
        self._refresh_labels()
        await interaction.response.edit_message(
            embed=status_embed(self.bot, self.channel_id, self.guild_id, self.channel_name), view=self
        )

    async def select_persona(self, interaction: discord.Interaction) -> None:
        persona = self.bot.personas.get(self.guild_id, (interaction.data or {}).get("values", [""])[0])
        if persona is None:
            await interaction.response.send_message("That persona isn't available in this server.", ephemeral=True)
            return
        self.bot.store.update_channel(self.channel_id, persona=persona.key)
        await self._refresh(interaction)

    @discord.ui.button(label="Detail", style=discord.ButtonStyle.secondary, row=2)
    async def verbosity_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        current = self.bot.store.channel_settings(self.channel_id).verbosity
        index = VERBOSITY_LEVELS.index(current) if current in VERBOSITY_LEVELS else -1
        next_level = VERBOSITY_LEVELS[(index + 1) % len(VERBOSITY_LEVELS)]
        self.bot.store.update_channel(self.channel_id, verbosity=next_level)
        await self._refresh(interaction)

    @discord.ui.button(label="Reactions", style=discord.ButtonStyle.secondary, row=2)
    async def reaction_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if not interaction.permissions.manage_messages:
            await interaction.response.send_message("Manage Messages permission is required.", ephemeral=True)
            return
        enabled = self.bot.store.channel_settings(self.channel_id).reactions
        self.bot.store.update_channel(self.channel_id, reactions=not enabled)
        await self._refresh(interaction)

    @discord.ui.button(label="Voice", style=discord.ButtonStyle.secondary, row=2)
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

    @discord.ui.button(label="Sounds", style=discord.ButtonStyle.secondary, row=3)
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

    @discord.ui.button(label="Reset history", style=discord.ButtonStyle.danger, row=2)
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

    @app_commands.command(name="timeout", description="Make the bot ignore someone for a while (moderators)")
    @app_commands.default_permissions(moderate_members=True)
    @app_commands.describe(member="Who to ignore", duration="How long: 30m, 2h, 1d (up to 7d), or off")
    async def timeout(self, interaction: discord.Interaction, member: discord.Member, duration: str) -> None:
        send = interaction.response.send_message
        perms = interaction.permissions
        if interaction.guild is None or not (perms and (perms.moderate_members or perms.manage_guild)):
            await send("Only moderators can time people out from the bot.", ephemeral=True)
            return
        if duration.strip().casefold() in {"off", "0", "none", "clear"}:
            cleared = self.bot.store.clear_timeout(interaction.guild.id, member.id)
            await send(
                f"{member.mention} can talk to me again." if cleared else f"{member.mention} wasn't timed out.",
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        seconds = parse_duration(duration)
        if seconds is None:
            await send("Give a duration like `30m`, `2h` or `1d` (up to 7 days), or `off`.", ephemeral=True)
            return
        until = time.time() + seconds
        self.bot.store.set_timeout(interaction.guild.id, member.id, until, interaction.user.id)
        await send(
            f"Ignoring {member.mention} until <t:{int(until)}:t> (<t:{int(until)}:R>).",
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @app_commands.command(name="reset", description="Clear this channel's conversation history")
    async def reset(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(**reset_prompt(self.bot, interaction))


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(SettingsCommands(bot))
