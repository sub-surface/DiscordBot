"""/persona, and /persona-manage: create, edit, delete and picture personas from one command."""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import PsychographBot
from ..personas import CUSTOM_PROMPT_LIMIT, Persona, can_manage
from ..render import CHOICE_LIMIT, EMBED_COLOR, PERSONA_LEGEND, persona_choices
from ..webhooks import AVATAR_MAX_BYTES, AVATAR_TYPES, member_named, square_png


def _manage_guild(interaction: discord.Interaction) -> bool:
    return bool(interaction.permissions and interaction.permissions.manage_guild)


async def _is_member_name(guild: discord.Guild, name: str) -> bool:
    """Bounded so a modal can still answer within Discord's 3 seconds; posting re-checks anyway."""
    try:
        return await asyncio.wait_for(member_named(guild, name), timeout=2)
    except asyncio.TimeoutError:
        return False


class CustomPersonaModal(discord.ui.Modal):
    """Creates a persona, or edits `persona` when given."""

    def __init__(self, bot: PsychographBot, persona: Persona | None = None) -> None:
        super().__init__(title="Edit custom persona" if persona else "Create custom persona", timeout=600)
        self.bot = bot
        self.persona = persona
        self.name_input = discord.ui.TextInput(default=persona.name if persona else None, max_length=40, required=True)
        self.prompt_input = discord.ui.TextInput(
            style=discord.TextStyle.paragraph,
            default=persona.prompt if persona else None,
            max_length=CUSTOM_PROMPT_LIMIT,
            required=True,
        )
        self.add_item(discord.ui.Label(text="Persona name", component=self.name_input))
        self.add_item(discord.ui.Label(text="Voice and instructions", component=self.prompt_input))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        send = interaction.response.send_message
        if interaction.guild_id is None:
            await send("Custom personas can only be managed in a server.", ephemeral=True)
            return
        if self.persona and (
            interaction.guild_id != self.persona.guild_id
            or not can_manage(self.persona, interaction.user.id, _manage_guild(interaction))
        ):
            await send("Only its creator or a server manager can edit this persona.", ephemeral=True)
            return

        name = str(self.name_input.value).strip()
        prompt = str(self.prompt_input.value).strip()
        if not name or not prompt:
            await send("Enter both a name and persona instructions.", ephemeral=True)
            return
        if self.bot.personas.is_reserved(name):
            await send("That name is reserved by a built-in persona.", ephemeral=True)
            return
        if interaction.guild and await _is_member_name(interaction.guild, name):
            await send(
                "Someone in this server goes by that name — pick a persona name that isn't a real member.",
                ephemeral=True,
            )
            return

        store = self.bot.store
        if self.persona is None:
            persona_id = store.create_custom_persona(interaction.guild_id, interaction.user.id, name, prompt)
            if persona_id is None:
                await send("A persona with that name already exists in this server.", ephemeral=True)
                return
            store.update_channel(interaction.channel_id, persona=f"custom:{persona_id}")
            await send(f"Created and selected **{name}** for this channel.", ephemeral=True)
        elif store.update_custom_persona(self.persona.custom_id, interaction.guild_id, name, prompt):
            await send(f"Updated custom persona **{name}**.", ephemeral=True)
        else:
            await send("Couldn't update that persona; another persona may already use that name.", ephemeral=True)


class PersonaDeleteView(discord.ui.View):
    def __init__(self, bot: PsychographBot, persona: Persona, requester_id: int) -> None:
        super().__init__(timeout=60)
        self.bot = bot
        self.persona = persona
        self.requester_id = requester_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id or _manage_guild(interaction):
            return True
        await interaction.response.send_message("Only the requester or a server manager can confirm this.", ephemeral=True)
        return False

    @discord.ui.button(label="Delete persona", style=discord.ButtonStyle.danger)
    async def confirm_delete(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.stop()
        allowed = can_manage(self.persona, interaction.user.id, _manage_guild(interaction))
        default = self.bot.personas.default_key
        avatar = self.bot.store.persona_avatar(self.persona.guild_id, self.persona.key) if self.persona.guild_id else None
        deleted = allowed and self.bot.personas.delete(self.persona)
        message = (
            f"Deleted **{self.persona.name}**. Channels using it returned to **{default}**."
            if deleted
            else "Couldn't delete that persona; check your permissions."
        )
        await interaction.response.edit_message(content=message, view=None)
        if deleted and avatar:
            await self.bot.webhooks.drop_avatar(avatar["webhook_id"])

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_delete(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(content="Deletion cancelled.", view=None)


NEW_PERSONA = "__new__"


def _may_picture(persona: Persona, interaction: discord.Interaction) -> bool:
    """Custom personas: their creator or a server manager. Built-ins are shared, so server managers only."""
    if persona.custom_id is not None:
        return can_manage(persona, interaction.user.id, _manage_guild(interaction))
    return _manage_guild(interaction)


def manage_embed(bot: PsychographBot, persona: Persona, guild_id: int) -> discord.Embed:
    uploaded = bot.store.persona_avatar(guild_id, persona.key) is not None
    summary = persona.about or (persona.prompt[:300] + ("…" if len(persona.prompt) > 300 else ""))
    embed = discord.Embed(title=f"{persona.label}", description=summary or None, color=EMBED_COLOR)
    embed.set_thumbnail(url=persona.avatar_url)
    embed.add_field(name="Group", value=persona.group.title(), inline=True)
    embed.add_field(name="Picture", value="Uploaded" if uploaded else "Generated", inline=True)
    if persona.creator_id:
        embed.add_field(name="Creator", value=f"<@{persona.creator_id}>", inline=True)
    embed.set_footer(text=f"New picture: /persona-manage name:{persona.name} image:<file>")
    return embed


class ManageView(discord.ui.View):
    """Edit, delete and picture controls for one persona; only the ones this person may use are shown."""

    def __init__(self, bot: PsychographBot, persona: Persona, interaction: discord.Interaction) -> None:
        super().__init__(timeout=300)
        self.bot = bot
        self.persona = persona
        self.requester_id = interaction.user.id
        editable = can_manage(persona, interaction.user.id, _manage_guild(interaction))
        uploaded = bot.store.persona_avatar(interaction.guild_id, persona.key) is not None
        if not editable:
            self.remove_item(self.edit_button)
            self.remove_item(self.delete_button)
        if not (uploaded and _may_picture(persona, interaction)):
            self.remove_item(self.reset_button)

    @property
    def empty(self) -> bool:
        return not self.children

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.requester_id:
            return True
        await interaction.response.send_message("Run `/persona-manage` to open your own controls.", ephemeral=True)
        return False

    @discord.ui.button(label="Edit", emoji="✏️", style=discord.ButtonStyle.primary)
    async def edit_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.send_modal(CustomPersonaModal(self.bot, self.persona))

    @discord.ui.button(label="Delete", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.stop()
        await interaction.response.edit_message(
            content=f"Delete **{self.persona.name}**? Channels using it will return to **{self.bot.personas.default_key}**.",
            embed=None,
            view=PersonaDeleteView(self.bot, self.persona, interaction.user.id),
        )

    @discord.ui.button(label="Reset picture", emoji="🖼️", style=discord.ButtonStyle.secondary)
    async def reset_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.stop()
        guild_id = interaction.guild_id
        existing = self.bot.store.persona_avatar(guild_id, self.persona.key)
        self.bot.store.delete_persona_avatar(guild_id, self.persona.key)
        await interaction.response.edit_message(
            content=f"**{self.persona.name}** is back to its generated avatar.", embed=None, view=None
        )
        if existing:
            await self.bot.webhooks.drop_avatar(existing["webhook_id"])


class PersonaCommands(commands.Cog):
    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot

    # `name` is required so Discord opens the persona list as soon as /persona is picked
    # (an optional one needs another Tab). The list marks the current persona; /status shows it too.
    @app_commands.command(name="persona", description="Choose this channel's persona")
    @app_commands.describe(name=PERSONA_LEGEND)
    async def persona(self, interaction: discord.Interaction, name: str) -> None:
        persona = self.bot.personas.find(interaction.guild_id, name)
        if persona is None:
            await interaction.response.send_message("That persona isn't available in this server.", ephemeral=True)
            return
        self.bot.store.update_channel(interaction.channel_id, persona=persona.key)
        notes = [persona.about] if persona.about else []
        if not self.bot.responder.can_run(persona):
            notes.append(self.bot.responder.tools_unavailable(persona))
        details = "".join(f"\n-# {note}" for note in notes)
        await interaction.response.send_message(f"This channel now uses **{persona.label}**.{details}", ephemeral=True)

    @persona.autocomplete("name")
    async def persona_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        registry = self.bot.personas
        selected = registry.for_channel(interaction.channel_id, interaction.guild_id).key
        return persona_choices(registry.search(interaction.guild_id, current), selected)

    @app_commands.command(name="persona-manage", description="Create a persona, or edit, delete or picture one")
    @app_commands.describe(
        name="＋ New persona, or one to manage",
        image="A new profile picture: PNG, JPG, GIF or WebP up to 8 MB, cropped to a square",
    )
    async def persona_manage(self, interaction: discord.Interaction, name: str, image: discord.Attachment | None = None) -> None:
        send = interaction.response.send_message
        guild = interaction.guild
        if guild is None:
            await send("Personas can only be managed in a server.", ephemeral=True)
            return
        if name == NEW_PERSONA:
            await interaction.response.send_modal(CustomPersonaModal(self.bot))
            return
        persona = self.bot.personas.find(guild.id, name)
        if persona is None or persona.mode != "chat":
            await send("Choose a persona from the list.", ephemeral=True)
            return
        if image is not None:
            await self._set_picture(interaction, persona, image)
            return
        view = ManageView(self.bot, persona, interaction)
        await send(
            embed=manage_embed(self.bot, persona, guild.id),
            view=None if view.empty else view,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @persona_manage.autocomplete("name")
    async def persona_manage_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """＋ New persona first, then the personas this person can change: their own customs, or all for managers."""
        manager = _manage_guild(interaction)
        personas = [
            persona
            for persona in self.bot.personas.search(interaction.guild_id, current)
            if persona.mode == "chat" and (manager or can_manage(persona, interaction.user.id, manager))
        ]
        new = [app_commands.Choice(name="＋ New persona", value=NEW_PERSONA)] if "new".startswith(current.strip().casefold()[:3]) else []
        return (new + persona_choices(personas))[:CHOICE_LIMIT]

    async def _set_picture(self, interaction: discord.Interaction, persona: Persona, image: discord.Attachment) -> None:
        send = interaction.response.send_message
        guild = interaction.guild
        if not _may_picture(persona, interaction):
            who = "its creator or a server manager" if persona.custom_id is not None else "a server manager"
            await send(f"Only {who} can change **{persona.name}**'s picture.", ephemeral=True)
            return
        content_type = (image.content_type or "").split(";")[0].strip().lower()
        if content_type not in AVATAR_TYPES or image.size > AVATAR_MAX_BYTES:
            await send("Use a PNG, JPG, GIF or WebP image up to 8 MB.", ephemeral=True)
            return
        if not self.bot.webhooks.available(interaction.channel):
            await send("I need the **Manage Webhooks** permission in this channel to keep pictures.", ephemeral=True)
            return

        existing = self.bot.store.persona_avatar(guild.id, persona.key)
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            png = await asyncio.to_thread(square_png, await image.read())
            url, webhook_id = await self.bot.webhooks.host_avatar(
                interaction.channel, persona, png, existing["webhook_id"] if existing else None
            )
        except ValueError as error:
            await interaction.followup.send(str(error), ephemeral=True)
            return
        except (discord.HTTPException, discord.ClientException):
            await interaction.followup.send(
                "Discord wouldn't save that picture. A channel can hold at most 15 webhooks — try another channel.",
                ephemeral=True,
            )
            return
        self.bot.store.set_persona_avatar(guild.id, persona.key, url, webhook_id, interaction.user.id)
        embed = discord.Embed(
            title=f"{persona.reaction} {persona.name}",
            description="New picture saved. Embeds use it now; turn on **Voice** in `/status` to post as this persona.",
            color=EMBED_COLOR,
        )
        embed.set_thumbnail(url=url)
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(PersonaCommands(bot))
