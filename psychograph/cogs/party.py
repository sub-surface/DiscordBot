"""Party tools: tabletop dice for now. No language model, no GPU.

/roll, "@bot roll 2d6+3", or a message that is only a roll ("roll d20 for stealth"); see dice.py.
On ready, the die icons are uploaded once as application emojis (see icons.py).
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from .. import dice, icons
from ..actions import act
from ..bot import PsychographBot

ROLL_SUGGESTIONS = ("d20", "adv", "dis", "2d6", "d20+5", "4d6kh3", "6x 4d6kh3", "8d6 for fireball", "d%", "d6!")


def roll_text(bot: PsychographBot, notation: str, who: str) -> str:
    """The reply to a roll: the result, or what was wrong with the notation."""
    try:
        result = dice.roll(notation)
    except dice.DiceError as error:
        return f"{icons.icon(bot, 'die_d20')} {error}"
    return dice.render(result, who, icons.icon(bot, dice.die_icon_name(result.biggest_die)))


async def reply_roll(bot: PsychographBot, message: discord.Message, notation: str) -> None:
    who = getattr(message.author, "display_name", None) or message.author.name
    await message.reply(roll_text(bot, notation, who), mention_author=False, allowed_mentions=discord.AllowedMentions.none())
    act("@", message.channel, message.author, f"rolled {notation[:40]}")


class PartyCog(commands.Cog):
    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot
        self._synced = False

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self._synced:
            self._synced = True
            await icons.sync(self.bot)

    @app_commands.command(name="roll", description="Roll dice: d20, 2d6+3, 4d6kh3, adv+5, 6x 4d6kh3")
    @app_commands.describe(dice="Dice notation, e.g. 2d6+3, 4d6kh3, adv+5, d%", reason="What it's for, e.g. stealth")
    async def roll(self, interaction: discord.Interaction, dice: str = "d20", reason: str = "") -> None:
        notation = f"{dice} for {reason}" if reason.strip() else dice
        who = getattr(interaction.user, "display_name", None) or interaction.user.name
        await interaction.response.send_message(roll_text(self.bot, notation, who), allowed_mentions=discord.AllowedMentions.none())

    @roll.autocomplete("dice")
    async def roll_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        typed = current.strip()
        options = ([typed] if typed else []) + [option for option in ROLL_SUGGESTIONS if typed.lower() in option]
        return [app_commands.Choice(name=option, value=option) for option in dict.fromkeys(options)][:25]


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(PartyCog(bot))
