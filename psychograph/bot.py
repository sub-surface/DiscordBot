from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from .backends import Backend, LocalBackend, make_backend
from .chess_game import ChessService, Stockfish
from .personas import PersonaRegistry
from .settings import Settings, load_settings
from .store import Store

log = logging.getLogger("psychograph")


def is_allowed_channel(channel: object | None, allowed: tuple[str, ...]) -> bool:
    """Threads follow their parent channel; DMs have no name and are never allowed."""
    channel = getattr(channel, "parent", None) or channel
    name = getattr(channel, "name", None)
    return isinstance(name, str) and name.casefold() in {item.casefold() for item in allowed}


class ChannelScopedCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = self.client.settings.allowed_channels
        if is_allowed_channel(interaction.channel, allowed):
            return True
        if interaction.guild is None:
            return False
        if interaction.type is discord.InteractionType.autocomplete:
            await interaction.response.autocomplete([])
        else:
            await interaction.response.send_message(
                f"I only respond in {', '.join(f'#{name}' for name in allowed)}.", ephemeral=True
            )
        return False


class PsychographBot(commands.Bot):
    def __init__(self, settings: Settings, store: Store | None = None, backend: Backend | None = None) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, tree_cls=ChannelScopedCommandTree)
        self.settings = settings
        self.store = store or Store(settings.db_path)
        self.backend = backend or make_backend(settings)
        self.personas = PersonaRegistry(self.store, settings.personas_dir, settings.default_persona)
        # Chess commentary only ever uses local LM Studio, whichever backend chat uses.
        commentator = self.backend if isinstance(self.backend, LocalBackend) else LocalBackend(settings)
        self.chess = ChessService(self.store, Stockfish(settings), commentator)
        self._legacy_guild_commands_cleared = False

    async def load_cogs(self) -> None:
        from .cogs import chat, chess, ops, personas, settings

        for module in (chat, chess, ops, personas, settings):
            await module.setup(self)

    async def setup_hook(self) -> None:
        await self.load_cogs()
        await self.tree.sync()

    async def on_ready(self) -> None:
        if self._legacy_guild_commands_cleared:
            return
        failed = False
        for guild in self.guilds:
            try:
                await self.tree.sync(guild=guild)
            except discord.HTTPException:
                failed = True
                log.exception("Failed to clear legacy guild commands from %s (%s)", guild.name, guild.id)
        if not failed:
            self._legacy_guild_commands_cleared = True
            log.info("Cleared legacy guild-scoped commands from %d guild(s)", len(self.guilds))

    async def on_message(self, message: discord.Message) -> None:
        # Slash commands only: ChatCog handles messages, so skip prefix-command processing.
        return

    async def close(self) -> None:
        await self.backend.close()
        if self.chess.commentator is not None and self.chess.commentator is not self.backend:
            await self.chess.commentator.close()
        self.chess.engine.close()
        await super().close()
        self.store.close()


def main() -> None:
    settings = load_settings()
    if not settings.discord_token:
        raise RuntimeError("DISCORD_TOKEN is not set")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    PsychographBot(settings).run(settings.discord_token)
