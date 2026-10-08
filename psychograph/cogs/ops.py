"""/bot model | stats | cost | thinking | digest: what the bot runs on, how it's doing, what it costs, whether the
model thinks before answering, and the digest preview."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from .. import digest
from ..bot import PsychographBot
from ..render import EMBED_COLOR, model_summary
from ..settings import ROOT
from ..stats import PERIODS, stats_lines

log = logging.getLogger("psychograph.ops")


def _modal_json(*args: str) -> object:
    """Run a Modal CLI command that prints JSON (blocking; run it in a thread)."""
    local_modal = ROOT / "venv" / "Scripts" / "modal.exe"
    executable = str(local_modal) if local_modal.exists() else shutil.which("modal")
    if not executable:
        raise RuntimeError("Modal CLI is not installed in the bot environment.")
    try:
        result = subprocess.run(
            [executable, *args, "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("Modal billing query timed out.") from None
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Modal billing query failed.")
    return json.loads(result.stdout)


def read_modal_billing(app_name: str) -> dict:
    """This month's workspace bill, and how much of it was this bot's app (the workspace hosts other apps too)."""
    summary = _modal_json("billing", "summary", "--for", "this month")
    start = datetime.now(timezone.utc).strftime("%Y-%m-01")
    rows = _modal_json("billing", "report", "--start", start)
    app_cost = sum(float(row.get("cost") or 0) for row in rows if row.get("description") == app_name) if isinstance(rows, list) else None
    return {**summary, "app_cost": app_cost}


class OpsCommands(commands.Cog):
    about = app_commands.Group(name="bot", description="About the bot: its model, stats and costs")

    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot

    @about.command(name="model", description="Which model the bot runs on, and how it's tuned")
    async def model(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(model_summary(self.bot.backend), ephemeral=True)

    @about.command(name="stats", description="Reply counts, speed and cold starts in this server")
    @app_commands.describe(period="How far back to look")
    @app_commands.choices(period=[app_commands.Choice(name=name.title(), value=name) for name in PERIODS])
    async def stats(self, interaction: discord.Interaction, period: str = "day") -> None:
        lines = stats_lines(self.bot.store.generation_stats(PERIODS[period], interaction.guild_id))
        embed = discord.Embed(title=f"Last {period}", description="\n".join(lines), color=EMBED_COLOR)
        embed.set_footer(text=self.bot.backend.profile.describe())
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @about.command(name="cost", description="This month's Modal workspace bill (Manage Server)")
    async def cost(self, interaction: discord.Interaction) -> None:
        if interaction.guild and not interaction.permissions.manage_guild:
            await interaction.response.send_message("You need Manage Server permission to view workspace costs.", ephemeral=True)
            return
        if self.bot.backend.name != "modal":
            await interaction.response.send_message(
                "Modal cost reporting is available when the bot uses the Modal backend.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            report = await asyncio.to_thread(read_modal_billing, self.bot.settings.modal_app)
            metered = float(report.get("metered_cost", 0))
            billed = float(report.get("billed_cost", 0))
            app_cost = report.get("app_cost")
        except (RuntimeError, ValueError) as error:
            log.exception("Modal billing lookup failed")
            await interaction.followup.send(f"Couldn't read Modal costs: {error}", ephemeral=True)
            return
        bot_line = f"This bot: **${app_cost:.2f}** (through yesterday). " if app_cost is not None else ""
        await interaction.followup.send(
            f"{bot_line}Modal workspace this month (all apps): **${metered:.2f} metered**, "
            f"**${billed:.2f} billed after credits**.",
            ephemeral=True,
        )

    @about.command(name="thinking", description="Show or switch the model's thinking mode (switching needs Manage Server)")
    @app_commands.describe(mode="On: the model reasons before replying (slower). Off: it answers straight away.")
    @app_commands.choices(mode=[app_commands.Choice(name="On", value="on"), app_commands.Choice(name="Off", value="off")])
    async def thinking(self, interaction: discord.Interaction, mode: str | None = None) -> None:
        backend = self.bot.backend
        if mode is not None:
            if interaction.guild is None or not interaction.permissions.manage_guild:  # a global switch: fail closed
                await interaction.response.send_message("You need Manage Server permission to switch thinking.", ephemeral=True)
                return
            backend.thinking = mode == "on"
            self.bot.store.set_state("thinking", mode)
        state = "**on**: the model reasons before it replies" if backend.thinking else "**off**: the model answers straight away"
        await interaction.response.send_message(f"Thinking is {state}.", ephemeral=True)

    @about.command(name="digest", description="Preview this week's Monday digest, just for you (Manage Server)")
    async def digest_preview(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not interaction.permissions.manage_guild:
            await interaction.response.send_message("You need Manage Server permission to preview the digest.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        text = await digest.build(self.bot, interaction.guild)
        who = digest.voice(self.bot, interaction.guild.id).name
        await interaction.followup.send(
            f"-# Posted on Monday as **{who}**:\n{text}"[:2000], ephemeral=True, allowed_mentions=discord.AllowedMentions.none()
        )


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(OpsCommands(bot))
