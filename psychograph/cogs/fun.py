"""/duel, the heartbeat and the Monday digest: the things the bot does that nobody asked for one by one.

A five-minute ticker (started once the bot is ready) fires the heartbeat's slots in each configured
channel, refreshes the status line hourly (sometimes a persona's line), and on Monday from 09:00 UTC posts the
week's digest to the digest channel once. A slot that comes due while the GPU is cold waits for the next
`model_warm` event (dispatched after every model reply) rather than waking it; see heartbeat.WARM_WAIT.
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import digest, duel, heartbeat
from ..bot import PsychographBot, is_allowed_channel
from ..conversation import clip
from ..personas import Persona
from ..render import EMBED_COLOR, persona_choices
from ..speak import generate, speak
from .. import quick as answers

log = logging.getLogger("psychograph.fun")

DIGEST_HOUR = 9   # UTC, Mondays


def parse_hours(text: str) -> tuple[int, int]:
    """ "13-1" → (13, 1): a UTC window that may wrap midnight."""
    try:
        start, end = (int(part) % 24 for part in text.split("-", 1))
        return start, end
    except ValueError:
        return 13, 1


class FunCommands(commands.Cog):
    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot
        self._dueling: set[int] = set()
        self._fired: set[str] = set()
        self._waiting: dict[str, tuple[discord.TextChannel, datetime]] = {}  # slot key → (channel, give up at)
        self._rng = random.Random()

    async def cog_unload(self) -> None:
        self.ticker.cancel()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.ticker.is_running():
            self.ticker.start()

    # ── Duels ───────────────────────────────────────────────────────

    def _duelist(self, guild_id: int | None, key: str) -> Persona | None:
        persona = self.bot.personas.find(guild_id, key)
        return persona if persona is not None and persona.mode == "chat" and persona.group != "tools" else None

    @app_commands.command(name="duel", description="Two personas argue a topic in rounds; Jev judges")
    @app_commands.describe(first="One persona", second="The other", topic="What they argue about", rounds="1 to 3 (default 2)")
    async def duel(
        self, interaction: discord.Interaction, first: str, second: str, topic: str,
        rounds: app_commands.Range[int, 1, duel.MAX_ROUNDS] = 2,
    ) -> None:
        send = interaction.response.send_message
        a, b = self._duelist(interaction.guild_id, first), self._duelist(interaction.guild_id, second)
        if a is None or b is None or a.key == b.key:
            await send("Pick two different chat personas.", ephemeral=True)
            return
        channel = interaction.channel
        if channel.id in self._dueling:
            await send("A duel is already running in this channel.", ephemeral=True)
            return
        self._dueling.add(channel.id)
        try:
            await send(
                f"**{a.name}** vs **{b.name}**, {rounds} round{'s' * (rounds > 1)}: *{discord.utils.escape_mentions(topic)}*",
                allowed_mentions=discord.AllowedMentions.none(),  # the topic is user text: no @everyone via the bot
            )
            turns: list[duel.Turn] = []
            for round_no in range(1, rounds + 1):
                for speaker, opponent in ((a, b), (b, a)):
                    context, instruction = duel.instruction(speaker, opponent, topic, turns, round_no, rounds)
                    text = await generate(self.bot, speaker, instruction, context, speakers=[a.name, b.name])
                    if not text:
                        await channel.send(
                            f"{speaker.name} couldn't answer (the model failed), so the duel ends here.",
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        return
                    await speak(self.bot, channel, speaker, text)
                    turns.append(duel.Turn(speaker, text))
            verdict = await duel.judge(self.bot.jev, topic, a, b, turns)
            await channel.send(embed=verdict_card(a, b, topic, verdict))
            if verdict.shares:
                self.bot.store.record_duel(
                    interaction.guild_id, channel.id, a.key, b.key, verdict.winner.key if verdict.winner else None, topic
                )
        finally:
            self._dueling.discard(channel.id)

    @duel.autocomplete("first")
    @duel.autocomplete("second")
    async def duel_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        personas = [
            p for p in self.bot.personas.search(interaction.guild_id, current) if p.mode == "chat" and p.group != "tools"
        ]
        return persona_choices(personas)

    # ── Heartbeat and digest ────────────────────────────────────────

    @tasks.loop(minutes=5)
    async def ticker(self) -> None:
        now = datetime.now(timezone.utc)
        try:
            await self._beat(now)
            await self._digest(now)
            if now.minute < 5:  # once an hour: a fresh status line
                await self.bot.change_presence(activity=self.bot.presence(self._rng))
        except Exception:
            log.exception("Ticker failed")

    async def _beat(self, now: datetime) -> None:
        settings = self.bot.settings
        start, end = parse_hours(settings.heartbeat_hours)
        wanted = {name.casefold() for name in settings.heartbeat_channels}
        for guild in self.bot.guilds:
            for channel in guild.text_channels:
                if channel.name.casefold() not in wanted or not is_allowed_channel(channel, settings.allowed_channels):
                    continue
                for key in heartbeat.due(now, f"{guild.id}:{channel.name}", settings.heartbeat_per_day, start, end):
                    if key not in self._fired:
                        self._fired.add(key)
                        if self.bot.backend.likely_cold:
                            self._waiting[key] = (channel, now + heartbeat.WARM_WAIT)
                        else:
                            await heartbeat.drop(self.bot, channel)
        # Slots that waited out WARM_WAIT on a cold GPU: free drops only.
        for key, (channel, give_up) in list(self._waiting.items()):
            if now >= give_up and self._waiting.pop(key, None):
                await heartbeat.drop(self.bot, channel, allow_model=False)

    @commands.Cog.listener()
    async def on_model_warm(self) -> None:
        """The GPU just answered someone, so it's warm: run the slots that were waiting for it."""
        waiting, self._waiting = self._waiting, {}
        for channel, _give_up in waiting.values():
            try:
                await heartbeat.drop(self.bot, channel)
            except Exception:
                log.exception("Heartbeat drop failed in #%s", channel.name)

    async def _digest(self, now: datetime) -> None:
        key = digest.week_key(now)
        if now.weekday() != 0 or now.hour < DIGEST_HOUR or self.bot.store.state(key):
            return
        self.bot.store.set_state(key, now.isoformat())  # once, even if a post fails
        for guild in self.bot.guilds:
            channel = discord.utils.find(
                lambda c: c.name.casefold() == self.bot.settings.digest_channel.casefold(), guild.text_channels
            )
            if channel is not None:
                await speak(self.bot, channel, digest.voice(self.bot, guild.id), await digest.build(self.bot, guild, now))


def verdict_card(a: Persona, b: Persona, topic: str, verdict: duel.Verdict) -> discord.Embed:
    if not verdict.shares:
        return discord.Embed(title="No verdict", description="Jev couldn't be reached to judge this one.", color=EMBED_COLOR)
    title = f"{verdict.winner.name} wins" if verdict.winner else "A draw"
    rows = [(a.name, verdict.shares.get(a.name, 0.0)), (b.name, verdict.shares.get(b.name, 0.0)), ("even", verdict.shares.get("even", 0.0))]
    lines = [f"`{answers.bar(share)}` {share:>4.0%}  {name}" for name, share in rows]
    if verdict.best is not None:
        lines.append(f"\nLine of the duel, **{verdict.best.persona.name}**:\n> {clip(verdict.best.text, 240)}")
    embed = discord.Embed(title=title, description=f"*{topic}*\n" + "\n".join(lines), color=EMBED_COLOR)
    embed.set_footer(text="Judged by Jev · no model call · /scores duels")
    return embed


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(FunCommands(bot))
