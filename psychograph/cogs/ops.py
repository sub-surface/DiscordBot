"""/model and /cost: what the bot runs on and what it costs."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import PsychographBot
from ..settings import ROOT

log = logging.getLogger("psychograph.ops")


def read_modal_billing() -> dict:
    """This month's workspace billing via the Modal CLI (blocking; run it in a thread)."""
    local_modal = ROOT / "venv" / "Scripts" / "modal.exe"
    executable = str(local_modal) if local_modal.exists() else shutil.which("modal")
    if not executable:
        raise RuntimeError("Modal CLI is not installed in the bot environment.")
    try:
        result = subprocess.run(
            [executable, "billing", "summary", "--for", "this month", "--json"],
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


class OpsCommands(commands.Cog):
    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot

    @app_commands.command(name="model", description="Show the configured inference model")
    async def model(self, interaction: discord.Interaction) -> None:
        backend = self.bot.backend
        await interaction.response.send_message(
            f"Backend: **{backend.name}**\nModel: `{backend.label}`\n{backend.note}", ephemeral=True
        )

    @app_commands.command(name="cost", description="Show this month's Modal workspace usage")
    @app_commands.default_permissions(manage_guild=True)
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
            report = await asyncio.to_thread(read_modal_billing)
            metered = float(report.get("metered_cost", 0))
            billed = float(report.get("billed_cost", 0))
        except (RuntimeError, ValueError) as error:
            log.exception("Modal billing lookup failed")
            await interaction.followup.send(f"Couldn't read Modal costs: {error}", ephemeral=True)
            return
        await interaction.followup.send(
            f"Modal workspace usage this month: **${metered:.2f} metered**, **${billed:.2f} billed after credits**. "
            "This is workspace-wide across Modal apps, not just Psychograph.",
            ephemeral=True,
        )


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(OpsCommands(bot))
