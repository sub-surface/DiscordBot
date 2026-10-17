"""Party tools: tabletop dice and the Scrollands' characters. No language model, no GPU.

/roll, "@bot roll 2d6+3", or a message that is only a roll ("roll d20 for stealth"); see dice.py.
"roll cope for the door" is a check with your character's score (characters.py).
/character create is a form (name, class, faction, keepsake, look), then a step to roll or take your scores and
place them; /character sheet, or right-click a member → Apps → Character sheet, shows the drawn sheet.
On ready, the die icons are uploaded once as application emojis (see icons.py).
"""

from __future__ import annotations

import io
import random

import discord
from discord import app_commands
from discord.ext import commands

from .. import characters as rules
from .. import dice, icons, sheet
from ..actions import act
from ..bot import PsychographBot
from .help import dice_embed

ROLL_SUGGESTIONS = ("help", "mog", "cope", "aura", "lore", "d20", "adv", "dis", "2d6", "d20+5", "4d6kh3", "6x 4d6kh3", "d%")
NO_PINGS = discord.AllowedMentions.none()


def _who(user: discord.abc.User) -> str:
    return getattr(user, "display_name", None) or user.name


def load_character(bot: PsychographBot, guild_id: int | None, user_id: int) -> rules.Character | None:
    data = bot.store.character(guild_id, user_id)
    return rules.Character.from_dict(data) if data else None


def roll_reply(bot: PsychographBot, notation: str, who: str, roller: rules.Character | None = None) -> tuple[str, discord.File | None]:
    """The reply to a roll: its text and a picture of the result, or what was wrong. A stat word ("cope for …")
    is a check with the roller's character."""
    stat = rules.stat_request(notation)
    if stat is not None:
        if roller is None:
            return f"{icons.icon(bot, 'die_d20')} Make a character first with `/character create`, then roll `{stat[0]}`.", None
        result = rules.check(roller, *stat)
        tone = {"crit": "crit", "fumble": "fumble"}.get(result.band[0], "normal")
        picture = icons.roll_image([(20, str(result.total), tone)])
        return rules.render_check(result, roller, who), discord.File(io.BytesIO(icons.png(picture)), filename="roll.png")
    try:
        result = dice.roll(notation)
    except dice.DiceError as error:
        return f"{icons.icon(bot, 'die_d20')} {error}", None
    sides = int(dice.die_icon_name(result.biggest_die).removeprefix("die_d"))
    tone = dice.tone(result)
    picture = icons.roll_image([(sides, str(total), tone) for total in result.totals])
    return dice.render(result, who), discord.File(io.BytesIO(icons.png(picture)), filename="roll.png")


async def reply_roll(bot: PsychographBot, message: discord.Message, notation: str) -> None:
    roller = load_character(bot, message.guild.id if message.guild else None, message.author.id)
    text, picture = roll_reply(bot, notation, _who(message.author), roller)
    await message.reply(text, file=picture or discord.utils.MISSING, mention_author=False, allowed_mentions=NO_PINGS)
    act("@", message.channel, message.author, f"rolled {notation[:40]}")


async def sheet_file(character: rules.Character, member: discord.abc.User) -> discord.File:
    portrait = None
    try:
        portrait = await member.display_avatar.replace(size=512, format="png").read()
    except (discord.HTTPException, AttributeError, ValueError):
        pass
    return discord.File(io.BytesIO(sheet.png(character, _who(member), portrait)), filename="sheet.png")


# ── Creating a character ────────────────────────────────────────────

class CreateForm(discord.ui.Modal, title="A new adventurer"):
    """Step one: who they are. Scores come next, so the form stays short."""

    name = discord.ui.TextInput(label="Name", placeholder="Theophant the Unshirted", max_length=40)

    def __init__(self, cog: PartyCog) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.role = discord.ui.Label(text="Class", component=discord.ui.Select(options=[
            discord.SelectOption(label=role.name, value=role.key, description=f"{rules.STAT_NAMES[role.stat]} · {role.blurb}"[:100])
            for role in rules.CLASSES.values()
        ]))
        self.faction = discord.ui.Label(text="Faction", component=discord.ui.Select(options=[
            discord.SelectOption(label=faction.name, value=faction.key, description=f"{faction.nickname} · {faction.blurb}"[:100])
            for faction in rules.FACTIONS.values()
        ]))
        self.keepsake = discord.ui.Label(text="Keepsake", component=discord.ui.Select(options=[
            discord.SelectOption(label="Let fate choose", value="random", description="One at random from the table"),
            *(discord.SelectOption(label=item[:100], value=str(index)) for index, item in enumerate(rules.KEEPSAKES)),
        ]))
        self.look = discord.ui.TextInput(label="Look (optional)", style=discord.TextStyle.paragraph, required=False, max_length=200,
                                         placeholder="Shirtless on principle. Carries a bowl.")
        for item in (self.role, self.faction, self.keepsake, self.look):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        pick = self.keepsake.component.values[0]
        keepsake = random.choice(rules.KEEPSAKES) if pick == "random" else rules.KEEPSAKES[int(pick)]
        draft = {
            "name": self.name.value.strip(), "role": self.role.component.values[0], "faction": self.faction.component.values[0],
            "keepsake": keepsake, "look": (self.look.value or "").strip(),
        }
        view = ScoresView(self.cog, interaction.user.id, draft)
        await interaction.response.send_message(view.prompt(), view=view, ephemeral=True)


class ScoresView(discord.ui.View):
    """Step two: the spawnpill. Roll 4d6kh3 four times or take 15/13/11/9, then place each score."""

    def __init__(self, cog: PartyCog, user_id: int, draft: dict) -> None:
        super().__init__(timeout=900)
        self.cog, self.user_id, self.draft = cog, user_id, draft
        self.values: list[int] = []
        self.placed: dict[str, int] = {}

    def prompt(self) -> str:
        role = rules.CLASSES[self.draft["role"]]
        lines = [f"**{self.draft['name']}**, {role.name}. Now the spawnpill: your four scores."]
        if not self.values:
            lines.append("Roll `4d6kh3` four times and live with it, or take the safe set **15, 13, 11, 9**.")
        else:
            lines.append(f"Your scores: **{', '.join(map(str, self.values))}**. Place each one below. "
                         f"{role.name}s lean on **{rules.STAT_NAMES[role.stat]}**.")
            for stat in rules.STATS:
                lines.append(f"-# {rules.STAT_NAMES[stat]} · {rules.STAT_HINTS[stat]}")
        return "\n".join(lines)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.user_id

    def _assign_controls(self) -> None:
        self.clear_items()
        options = [discord.SelectOption(label=str(value), value=f"{index}:{value}") for index, value in enumerate(self.values)]
        for row, stat in enumerate(rules.STATS):
            select = discord.ui.Select(placeholder=f"{rules.STAT_NAMES[stat]} · {rules.STAT_HINTS[stat]}", options=options, row=row)
            select.callback = self._placer(stat, select)
            self.add_item(select)
        confirm = discord.ui.Button(label="Create character", style=discord.ButtonStyle.primary, row=4)
        confirm.callback = self._confirm
        self.add_item(confirm)

    def _placer(self, stat: str, select: discord.ui.Select):
        async def place(interaction: discord.Interaction) -> None:
            self.placed[stat] = select.values[0]
            await interaction.response.defer()
        return place

    @discord.ui.button(label="Roll the spawnpill", style=discord.ButtonStyle.primary)
    async def roll_scores(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.values = rules.spawnpill()
        self._assign_controls()
        await interaction.response.edit_message(content=self.prompt(), view=self)

    @discord.ui.button(label="Take 15, 13, 11, 9", style=discord.ButtonStyle.secondary)
    async def standard(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        self.values = list(rules.STANDARD_SET)
        self._assign_controls()
        await interaction.response.edit_message(content=self.prompt(), view=self)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        used = [self.placed.get(stat) for stat in rules.STATS]
        if None in used or len(set(used)) != len(used):
            await interaction.response.send_message("Place each score once: one per stat, none twice.", ephemeral=True)
            return
        scores = {stat: int(slot.split(":")[1]) for stat, slot in self.placed.items()}
        character = rules.Character(**self.draft, scores=scores, portrait=str(interaction.user.display_avatar.url))
        self.cog.bot.store.save_character(interaction.guild_id, interaction.user.id, character.to_dict())
        self.stop()
        await interaction.response.edit_message(content=f"**{character.name}** is ready.", view=None)
        role, faction = character.role_info, character.faction_info
        article = "an" if role.name[0] in "AEIOU" else "a"
        text = f"**{character.name}** enters the Scrollands: {article} {role.name} of {faction.name}."
        await interaction.followup.send(text, file=await sheet_file(character, interaction.user), allowed_mentions=NO_PINGS)
        act("^", interaction.channel, interaction.user, f"created character {character.name[:30]}")


class PartyCog(commands.Cog):
    character = app_commands.Group(name="character", description="Your adventurer in the Scrollands")

    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot
        self._synced = False
        self.sheet_menu = app_commands.ContextMenu(name="Character sheet", callback=self.sheet_of)
        self.bot.tree.add_command(self.sheet_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.sheet_menu.name, type=self.sheet_menu.type)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self._synced:
            self._synced = True
            await icons.sync(self.bot)

    @app_commands.command(name="roll", description="Roll dice (2d6+3, adv+5) or a check with your character (cope, mog adv)")
    @app_commands.describe(dice="Dice notation (2d6+3, 4d6kh3, adv+5) or a score (mog, cope, aura, lore)", reason="What it's for")
    async def roll(self, interaction: discord.Interaction, dice: str = "d20", reason: str = "") -> None:
        if dice.strip().lower() in ("help", "?"):
            await interaction.response.send_message(embed=dice_embed(self.bot), ephemeral=True)
            return
        notation = f"{dice} for {reason}" if reason.strip() else dice
        roller = load_character(self.bot, interaction.guild_id, interaction.user.id)
        text, picture = roll_reply(self.bot, notation, _who(interaction.user), roller)
        await interaction.response.send_message(text, file=picture or discord.utils.MISSING, allowed_mentions=NO_PINGS)

    @roll.autocomplete("dice")
    async def roll_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        typed = current.strip()
        options = ([typed] if typed else []) + [option for option in ROLL_SUGGESTIONS if typed.lower() in option]
        return [app_commands.Choice(name=option, value=option) for option in dict.fromkeys(options)][:25]

    @character.command(name="create", description="Make your adventurer: name, class, faction, keepsake, then scores")
    async def create(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CreateForm(self))

    @character.command(name="sheet", description="Show a character sheet")
    @app_commands.describe(member="Whose sheet (yours if left out)")
    async def sheet(self, interaction: discord.Interaction, member: discord.Member | None = None) -> None:
        await self._show(interaction, member or interaction.user)

    async def sheet_of(self, interaction: discord.Interaction, member: discord.Member) -> None:
        await self._show(interaction, member)

    async def _show(self, interaction: discord.Interaction, member: discord.abc.User) -> None:
        character = load_character(self.bot, interaction.guild_id, member.id)
        if character is None:
            mine = member.id == interaction.user.id
            await interaction.response.send_message(
                "You don't have a character yet: `/character create`." if mine else f"{_who(member)} hasn't made a character yet.",
                ephemeral=True,
            )
            return
        await interaction.response.defer()
        await interaction.followup.send(file=await sheet_file(character, member), allowed_mentions=NO_PINGS)


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(PartyCog(bot))
