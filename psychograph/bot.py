from __future__ import annotations

import logging
import random
import time

import discord
from discord import app_commands
from discord.ext import commands

from .ambient import Ambient
from .backends import Backend, LocalBackend, make_backend
from .chess_game import ChessService, Stockfish
from .jev import Jev
from .personas import PersonaRegistry
from .responder import Responder
from .settings import Settings, load_settings
from .sounds import Soundbank
from .store import Store
from .webhooks import PersonaWebhooks

log = logging.getLogger("psychograph")

QUOTE_SHARE = 0.5   # how often the hourly status line is a persona's line rather than the model


def is_allowed_channel(channel: object | None, allowed: tuple[str, ...]) -> bool:
    """Threads follow their parent channel; DMs have no name and are never allowed."""
    channel = getattr(channel, "parent", None) or channel
    name = getattr(channel, "name", None)
    return isinstance(name, str) and name.casefold() in {item.casefold() for item in allowed}


class ChannelScopedCommandTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        allowed = self.client.settings.allowed_channels
        if is_allowed_channel(interaction.channel, allowed):
            user = getattr(interaction, "user", None)
            until = self.client.ignoring(getattr(interaction, "guild_id", None), user.id) if user else None
            if until is None:
                return True
            if interaction.type is discord.InteractionType.autocomplete:
                await interaction.response.autocomplete([])
            else:
                await interaction.response.send_message(
                    f"You're timed out from me until <t:{int(until)}:t>.", ephemeral=True
                )
            return False
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
        # Thinking mode is off unless /bot thinking turned it on: faster replies, less GPU time.
        self.backend.thinking = self.store.state("thinking") == "on"
        self.personas = PersonaRegistry(self.store, settings.personas_dir, settings.default_persona)
        # Chess commentary only ever uses local LM Studio, whichever backend chat uses.
        commentator = self.backend if isinstance(self.backend, LocalBackend) else LocalBackend(settings)
        self.chess = ChessService(self.store, Stockfish(settings), commentator)
        self.webhooks = PersonaWebhooks(self)
        self.soundbank = Soundbank(settings.sounds_dir)
        self.jev = Jev(settings.jev_api_key, settings.jev_model)
        self.responder = Responder(self)
        self.ambient = Ambient(self)
        self._legacy_guild_commands_cleared = False

    def model_warmed(self) -> None:
        """The model just answered, so a Modal GPU is up for its idle window: let waiting work use it."""
        if self.is_ready():
            self.dispatch("model_warm")

    async def load_cogs(self) -> None:
        from .cogs import chat, chess, fun, help, ops, personas, quick, settings, slop, soundboard, tools

        for module in (chat, chess, fun, help, ops, personas, quick, settings, slop, soundboard, tools):
            await module.setup(self)

    def ignoring(self, guild_id: int | None, user_id: int) -> float | None:
        """When a /timeout on this member ends, if they're timed out from the bot right now."""
        return self.store.timeout_until(guild_id, user_id, time.time())

    def presence(self, rng: random.Random | None = None) -> discord.CustomActivity:
        """The status line: the model, or now and then (`rng` given) a real line from one of the personas."""
        lines = [(p.name, line) for p in self.personas.available(None) for line in p.says if len(line) <= 100]
        if rng is not None and lines and rng.random() < QUOTE_SHARE:
            name, line = rng.choice(lines)
            return discord.CustomActivity(f"{name}: “{line}”")
        profile = self.backend.profile
        model = profile.name.split(" (")[0] if profile.key != "default" else self.backend.label.rsplit("/", 1)[-1]
        return discord.CustomActivity(f"🧠 {model} · {profile.context_mode} context · /help")

    async def setup_hook(self) -> None:
        await self.load_cogs()
        await self.tree.sync()

    async def on_ready(self) -> None:
        try:
            await self.change_presence(activity=self.presence())
        except discord.HTTPException:
            log.info("Couldn't set presence")
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
        await self.jev.close()
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
    bot = PsychographBot(settings)
    log.info(
        "Psychograph starting · backend %s · %s · %s",
        bot.backend.name,
        bot.backend.label,
        bot.backend.profile.describe(),
    )
    bot.run(settings.discord_token, log_handler=None)
