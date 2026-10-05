"""/persona, custom persona management, and persona profile pictures."""

from __future__ import annotations

import asyncio

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import PsychographBot
from ..personas import CUSTOM_PROMPT_LIMIT, Persona, can_manage
from ..render import EMBED_COLOR
from ..webhooks import AVATAR_MAX_BYTES, AVATAR_TYPES, member_named, square_png


def _manage_guild(interaction: discord.Interaction) -> bool:
    return bool(interaction.permissions and interaction.permissions.manage_guild)


def _choices(personas: list[Persona]) -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=persona.name[:100], value=persona.key) for persona in personas][:25]


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


class PersonaCommands(commands.Cog):
    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot

    def _managed_persona(self, interaction: discord.Interaction, key: str) -> tuple[Persona | None, str | None]:
        """The custom persona `key` names, or an error message if it's missing or not the user's to manage."""
        persona = self.bot.personas.find(interaction.guild_id, key) if interaction.guild_id else None
        if persona is None or persona.custom_id is None:
            return None, "Choose a custom persona from this server."
        if not can_manage(persona, interaction.user.id, _manage_guild(interaction)):
            return None, "Only its creator or a server manager can change this persona."
        return persona, None

    async def _custom_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return _choices(self.bot.personas.search(interaction.guild_id, current, custom_only=True))

    @app_commands.command(name="persona", description="Show or choose this channel's persona")
    @app_commands.describe(name="Leave blank to view the current persona and available choices")
    async def persona(self, interaction: discord.Interaction, name: str | None = None) -> None:
        registry = self.bot.personas
        if name is None:
            current = registry.for_channel(interaction.channel_id, interaction.guild_id)
            available = ", ".join(persona.name for persona in registry.available(interaction.guild_id))
            await interaction.response.send_message(
                f"Current persona: **{current.name}**\nAvailable: {available}", ephemeral=True
            )
            return
        persona = registry.find(interaction.guild_id, name)
        if persona is None:
            await interaction.response.send_message("That persona isn't available in this server.", ephemeral=True)
            return
        self.bot.store.update_channel(interaction.channel_id, persona=persona.key)
        await interaction.response.send_message(f"This channel now uses **{persona.name}**.", ephemeral=True)

    @persona.autocomplete("name")
    async def persona_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return _choices(self.bot.personas.search(interaction.guild_id, current))

    @app_commands.command(name="persona-create", description="Create and select a custom server persona")
    async def persona_create(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Custom personas can only be created in a server.", ephemeral=True)
            return
        await interaction.response.send_modal(CustomPersonaModal(self.bot))

    @app_commands.command(name="persona-edit", description="Edit a custom persona you own")
    @app_commands.describe(name="Custom persona to edit")
    async def persona_edit(self, interaction: discord.Interaction, name: str) -> None:
        persona, error = self._managed_persona(interaction, name)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.send_modal(CustomPersonaModal(self.bot, persona))

    @app_commands.command(name="persona-delete", description="Delete a custom persona you own")
    @app_commands.describe(name="Custom persona to delete")
    async def persona_delete(self, interaction: discord.Interaction, name: str) -> None:
        persona, error = self._managed_persona(interaction, name)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        await interaction.response.send_message(
            f"Delete **{persona.name}**? Channels using it will return to **{self.bot.personas.default_key}**.",
            view=PersonaDeleteView(self.bot, persona, interaction.user.id),
            ephemeral=True,
        )

    persona_edit.autocomplete("name")(_custom_autocomplete)
    persona_delete.autocomplete("name")(_custom_autocomplete)

    @app_commands.command(name="persona-avatar", description="Give a persona its own profile picture, or reset it")
    @app_commands.describe(
        name="The persona",
        image="PNG, JPG, GIF or WebP up to 8 MB — cropped to a square",
        reset="Go back to the generated avatar",
    )
    async def persona_avatar(
        self,
        interaction: discord.Interaction,
        name: str,
        image: discord.Attachment | None = None,
        reset: bool = False,
    ) -> None:
        send = interaction.response.send_message
        guild = interaction.guild
        persona = self.bot.personas.find(guild.id, name) if guild else None
        if persona is None or persona.mode != "chat":
            await send("Choose a chat persona from this server.", ephemeral=True)
            return
        # Custom personas: their creator or a server manager. Built-ins are shared, so server managers only.
        allowed = (
            can_manage(persona, interaction.user.id, _manage_guild(interaction))
            if persona.custom_id is not None
            else _manage_guild(interaction)
        )
        existing = self.bot.store.persona_avatar(guild.id, persona.key)

        if image is None and not reset:
            embed = discord.Embed(
                title=persona.name,
                description="Uploaded picture." if existing else "Generated avatar — upload an `image` to change it.",
                color=EMBED_COLOR,
            )
            embed.set_thumbnail(url=persona.avatar_url)
            await send(embed=embed, ephemeral=True)
            return
        if not allowed:
            who = "its creator or a server manager" if persona.custom_id is not None else "a server manager"
            await send(f"Only {who} can change **{persona.name}**'s picture.", ephemeral=True)
            return
        if reset:
            self.bot.store.delete_persona_avatar(guild.id, persona.key)
            await send(f"**{persona.name}** is back to its generated avatar.", ephemeral=True)
            if existing:
                await self.bot.webhooks.drop_avatar(existing["webhook_id"])
            return

        content_type = (image.content_type or "").split(";")[0].strip().lower()
        if content_type not in AVATAR_TYPES or image.size > AVATAR_MAX_BYTES:
            await send("Use a PNG, JPG, GIF or WebP image up to 8 MB.", ephemeral=True)
            return
        if not self.bot.webhooks.available(interaction.channel):
            await send("I need the **Manage Webhooks** permission in this channel to keep pictures.", ephemeral=True)
            return

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

    @persona_avatar.autocomplete("name")
    async def persona_avatar_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return _choices([p for p in self.bot.personas.search(interaction.guild_id, current) if p.mode == "chat"])


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(PersonaCommands(bot))
