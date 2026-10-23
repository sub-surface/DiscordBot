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
import time
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import characters as rules
from .. import combat, dice, halloween, icons, ledger, sheet, tarot, world
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


def _picture(dice_list: list[tuple[int, str, str]], name: str = "roll.png") -> discord.File:
    return discord.File(io.BytesIO(icons.png(icons.roll_image(dice_list))), filename=name)


def _tone(natural: int) -> str:
    return "crit" if natural == 20 else "fumble" if natural == 1 else "normal"


def roll_reply(bot: PsychographBot, notation: str, who: str, roller: rules.Character | None = None) -> tuple[str, discord.File | None, bool]:
    """The reply to a roll: its text, a picture of the result (or None if something was wrong), and whether it
    went badly enough to be worth a Chuckle reroll. A score or skill ("cope for …") is a check with the roller's
    character; "attack [vs 13]" and "damage" use their class weapon (combat.py)."""
    fight = combat.request(notation)
    stat = None if fight else rules.stat_request(notation)
    if (fight or stat) and roller is None:
        word = fight[0] if fight else stat[0]
        return f"{icons.icon(bot, 'die_d20')} Make a character first with `/character create`, then roll `{word}`.", None, False
    if fight and fight[0] == "damage":
        faces, flat, total = combat.roll_damage(combat.damage_notation(roller))
        sides = combat.die_sides(combat.weapon(roller).dice)
        return combat.render_damage(roller, faces, flat, total, who), _picture([(sides, str(total), "normal")]), False
    if fight:
        _what, lean, ac, reason = fight
        result = combat.character_strike(roller, "", ac, lean, reason=reason)
        dice_list = [(20, str(result.total), _tone(result.kept))]
        if result.damage:
            dice_list.append((combat.die_sides(result.damage_dice), str(result.damage), "crit" if result.crit else "normal"))
        text = halloween.witnessed(combat.render(result, who), [_tone(result.kept)])
        return text, _picture(dice_list), result.hit is False and roller.chuckles > 0
    if stat is not None:
        result = rules.check(roller, *stat)
        retry = result.band[0] in ("miss", "but", "fumble") and roller.chuckles > 0
        text = halloween.witnessed(rules.render_check(result, roller, who), [_tone(result.natural)])
        return text, _picture([(20, str(result.total), _tone(result.natural))]), retry
    try:
        result = dice.roll(notation)
    except dice.DiceError as error:
        return f"{icons.icon(bot, 'die_d20')} {error}", None, False
    sides = int(dice.die_icon_name(result.biggest_die).removeprefix("die_d"))
    text = halloween.witnessed(dice.render(result, who), [dice.tone(result)])
    return text, _picture([(sides, str(total), dice.tone(result)) for total in result.totals]), False


class RerollView(discord.ui.View):
    """Under a roll that went badly: the roller may spend a Chuckle to roll it again (the new roll stands)."""

    def __init__(self, bot: PsychographBot, guild_id: int | None, roller_id: int, redo) -> None:
        super().__init__(timeout=600)
        self.bot, self.guild_id, self.roller_id, self.redo = bot, guild_id, roller_id, redo
        self.used = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.roller_id:
            await interaction.response.send_message("Only the one who rolled can spend their Chuckle on it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Spend a Chuckle · reroll", style=discord.ButtonStyle.secondary)
    async def reroll(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if self.used:  # a double click lands while the first is still editing the message
            await interaction.response.defer()
            return
        self.used = True  # set before any await, so only one click ever spends
        self.stop()
        character = load_character(self.bot, self.guild_id, self.roller_id)
        spent = character is not None and character.chuckles >= 1 and ledger.commit(
            self.bot.store, self.guild_id, [ledger.chuckles(self.roller_id, -1, "spent on a reroll")], "player")[0].data["n"] == -1
        if not spent:
            await interaction.response.send_message("No Chuckles left. Earn one: be funny, or suffer well.", ephemeral=True)
            return
        text, picture, _retry = await self.redo()
        left = character.chuckles - 1
        text += f"\n-# Rerolled with a Chuckle · {left} left"
        await interaction.response.edit_message(content=text, attachments=[picture] if picture else [], view=None)
        act("^", interaction.channel, interaction.user, "spent a Chuckle on a reroll")


async def send_roll(bot: PsychographBot, send, guild_id: int | None, roller_id: int, notation: str, who: str) -> None:
    """Roll and post through `send` (a reply or an interaction response), with the reroll button when it's earned."""
    roller = load_character(bot, guild_id, roller_id)
    text, picture, retry = roll_reply(bot, notation, who, roller)

    async def redo():
        return roll_reply(bot, notation, who, load_character(bot, guild_id, roller_id))

    view = RerollView(bot, guild_id, roller_id, redo) if retry else discord.utils.MISSING
    await send(text, file=picture or discord.utils.MISSING, view=view, allowed_mentions=NO_PINGS)


async def reply_roll(bot: PsychographBot, message: discord.Message, notation: str) -> None:
    async def send(text, **kwargs):
        await message.reply(text, mention_author=False, **kwargs)

    await send_roll(bot, send, message.guild.id if message.guild else None, message.author.id, notation, _who(message.author))
    act("@", message.channel, message.author, f"rolled {notation[:40]}")


async def deal_tarot(bot: PsychographBot, reading: tarot.Reading, send, edit, asker_id: int | None) -> None:
    """Deal a reading face down and turn it (tarot.deal), with Mini Me's take when it's on and the asker is due one."""
    await tarot.deal(reading, send, edit, tarot.take(bot, reading) if tarot.wants_take(bot, asker_id) else None)


async def answer_tarot(bot: PsychographBot, interaction: discord.Interaction, reading: tarot.Reading) -> None:
    async def send(view):
        await interaction.response.send_message(view=view, allowed_mentions=NO_PINGS)

    async def edit(view):
        await interaction.edit_original_response(view=view, allowed_mentions=NO_PINGS)

    await deal_tarot(bot, reading, send, edit, interaction.user.id)


async def reply_tarot(bot: PsychographBot, message: discord.Message, kind: str, question: str) -> None:
    """A quick reading asked for in chat ("draw a card", "tarot: will I win"): see tarot.request."""
    reading = tarot.reading(kind, message.guild.id if message.guild else None, _who(message.author), question)
    sent: list[discord.Message] = []

    async def send(view):
        sent.append(await message.reply(view=view, mention_author=False, allowed_mentions=NO_PINGS))

    async def edit(view):
        await sent[0].edit(view=view, allowed_mentions=NO_PINGS)

    act("@", message.channel, message.author, f"tarot {kind}")
    await deal_tarot(bot, reading, send, edit, message.author.id)


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
            discord.SelectOption(label=f"{role.name} ({role.classic})", value=role.key,
                                 description=f"{rules.STAT_NAMES[role.stat]} · {role.blurb}"[:100])
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
        ledger.commit(self.cog.bot.store, interaction.guild_id, [ledger.baseline(interaction.user.id, character.to_dict())], "player")
        self.stop()
        await interaction.response.edit_message(content=f"**{character.name}** is ready.", view=None)
        role, faction = character.role_info, character.faction_info
        article = "an" if role.name[0] in "AEIOU" else "a"
        text = f"**{character.name}** enters the Scrollands: {article} {role.name} of {faction.name}."
        await interaction.followup.send(text, file=await sheet_file(character, interaction.user), allowed_mentions=NO_PINGS)
        act("^", interaction.channel, interaction.user, f"created character {character.name[:30]}")


# ── Right-click actions ─────────────────────────────────────────────

CONTEST_CHECKS = ["mog", "cope", "aura", "lore", "athletics", "stealth", "persuasion", "deception", "perception", "insight"]
CRIER_EVERY = timedelta(hours=6)         # at most one Crier line this often
CRIER_QUIET_AFTER = timedelta(hours=48)  # and only while someone has spoken in the game channel lately


def contest_text(a: rules.Character, b: rules.Character, label: str, ra: rules.Check, rb: rules.Check, who_a: str, who_b: str) -> str:
    if ra.total == rb.total:
        verdict = "A stalemate. Nobody yields."
    else:
        verdict = f"**{(a if ra.total > rb.total else b).name}** wins."
    return "\n".join((
        f"### A contest of {label}",
        f"**{a.name}** {ra.total} · **{b.name}** {rb.total} · {verdict}",
        f"-# {a.name} ({who_a}) [{ra.kept}] {rules.signed(ra.bonus)} · {b.name} ({who_b}) [{rb.kept}] {rules.signed(rb.bonus)}",
    ))


class ContestView(discord.ui.View):
    """Pick what the contest is of; both characters roll it and the higher total wins."""

    def __init__(self, challenger: discord.abc.User, target: discord.abc.User, mine: rules.Character, theirs: rules.Character) -> None:
        super().__init__(timeout=300)
        self.challenger, self.target, self.mine, self.theirs = challenger, target, mine, theirs
        self.select = discord.ui.Select(placeholder="A contest of…", options=[
            discord.SelectOption(label=rules.stat_request(word)[3], value=word) for word in CONTEST_CHECKS
        ])
        self.select.callback = self._contest
        self.add_item(self.select)

    async def _contest(self, interaction: discord.Interaction) -> None:
        stat, _lean, _reason, label = rules.stat_request(self.select.values[0])
        ra, rb = rules.check(self.mine, stat, label=label), rules.check(self.theirs, stat, label=label)
        tones = [{"crit": "crit", "fumble": "fumble"}.get(r.band[0], "normal") for r in (ra, rb)]
        picture = icons.roll_image([(20, str(ra.total), tones[0]), (20, str(rb.total), tones[1])])
        self.stop()
        await interaction.response.edit_message(content="Thrown down.", view=None)
        text = contest_text(self.mine, self.theirs, label, ra, rb, _who(self.challenger), _who(self.target))
        await interaction.followup.send(f"{self.target.mention}\n{text}", file=discord.File(io.BytesIO(icons.png(picture)), filename="contest.png"),
                                        allowed_mentions=discord.AllowedMentions(users=[self.target]))
        act("^", interaction.channel, interaction.user, f"contest of {label} with {_who(self.target)[:20]}")


class PayForm(discord.ui.Modal, title="Pay keks"):
    amount = discord.ui.TextInput(label="How many keks", placeholder="25", max_length=7)
    note = discord.ui.TextInput(label="What for (optional)", required=False, max_length=80, placeholder="for the pizza")

    def __init__(self, bot: PsychographBot, target: discord.abc.User) -> None:
        super().__init__(timeout=300)
        self.bot, self.target = bot, target

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild_id
        payer, payee = load_character(self.bot, guild, interaction.user.id), load_character(self.bot, guild, self.target.id)
        try:
            amount = int(self.amount.value.strip().replace(",", ""))
        except ValueError:
            amount = 0
        reason = "Both of you need a character." if payer is None or payee is None else "" if amount > 0 else "Enter a whole number of keks."
        note = self.note.value.strip()
        if not reason:
            try:
                ledger.commit(self.bot.store, guild, [ledger.keks(interaction.user.id, -amount, note), ledger.keks(self.target.id, amount, note)], "player")
            except ledger.LedgerError as error:
                reason = f"Not enough: {error}"
        if reason:
            await interaction.response.send_message(reason, ephemeral=True)
            return
        note = f" {note}" if note else ""
        await interaction.response.send_message(f"-# {payer.name} paid {payee.name} **{amount} keks**{note}.", allowed_mentions=NO_PINGS)
        act("^", interaction.channel, interaction.user, f"paid {amount} keks to {_who(self.target)[:20]}")


class GiveForm(discord.ui.Modal, title="Hand over an item"):
    """Pick one of your items and how many: it leaves your pack and lands in theirs, both or neither."""

    count = discord.ui.TextInput(label="How many", default="1", max_length=4)

    def __init__(self, bot: PsychographBot, target: discord.abc.User, mine: rules.Character) -> None:
        super().__init__(timeout=300)
        self.bot, self.target = bot, target
        self.names = [item["name"] for item in mine.inventory[:25]]
        self.item = discord.ui.Label(text="Item", component=discord.ui.Select(options=[
            discord.SelectOption(label=item["name"][:100], value=str(index), description=f"×{item['qty']} · {item['kind']}"[:100])
            for index, item in enumerate(mine.inventory[:25])
        ]))
        self.add_item(self.item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = self.names[int(self.item.component.values[0])]
        try:
            count = int(self.count.value.strip())
            entries = ledger.commit(self.bot.store, interaction.guild_id, [
                ledger.lose(interaction.user.id, name, count, f"to {_who(self.target)}"),
                ledger.gain(self.target.id, name, count, note=f"from {_who(interaction.user)}"),
            ], "player")
        except ValueError as error:  # LedgerError is a ValueError too
            await interaction.response.send_message(str(error) if isinstance(error, ledger.LedgerError) else "Enter a whole number.", ephemeral=True)
            return
        giver = load_character(self.bot, interaction.guild_id, interaction.user.id)
        taker = load_character(self.bot, interaction.guild_id, self.target.id)
        amount = f" ×{count}" if count != 1 else ""
        await interaction.response.send_message(f"-# {giver.name} handed {taker.name} **{entries[1].data['item']}**{amount}.",
                                                allowed_mentions=NO_PINGS)
        act("^", interaction.channel, interaction.user, f"gave {name[:30]} to {_who(self.target)[:20]}")


class PartyCog(commands.Cog):
    character = app_commands.Group(name="character", description="Your adventurer in the Scrollands")
    world_group = app_commands.Group(name="world", description="The Scrollands: the map, and travel")

    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot
        self._synced = False
        self.menus = [
            app_commands.ContextMenu(name="Character sheet", callback=self.sheet_of),
            app_commands.ContextMenu(name="Challenge to a contest", callback=self.challenge),
            app_commands.ContextMenu(name="Pay keks", callback=self.pay),
            app_commands.ContextMenu(name="Hand over an item", callback=self.give_item),
            app_commands.ContextMenu(name="Attack", callback=self.attack),
            app_commands.ContextMenu(name="Give a Chuckle", callback=self.give_chuckle),
            app_commands.ContextMenu(name="Draw a tarot card", callback=self.card_for),
        ]
        for menu in self.menus:
            self.bot.tree.add_command(menu)

    async def cog_unload(self) -> None:
        self.crier.cancel()
        for menu in self.menus:
            self.bot.tree.remove_command(menu.name, type=menu.type)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self._synced:
            self._synced = True
            await icons.sync(self.bot)
        if not self.crier.is_running():
            self.crier.start()

    # ── The Crier: world status, now and then, while people are around ──

    def game_channel(self) -> discord.TextChannel | None:
        for guild in self.bot.guilds:
            found = discord.utils.get(guild.text_channels, name=self.bot.settings.game_channel)
            if found is not None:
                return found
        return None

    @tasks.loop(minutes=30)
    async def crier(self) -> None:
        last = float(self.bot.store.state("crier:last") or 0)
        if time.time() - last < CRIER_EVERY.total_seconds() or random.random() > 0.35:
            return
        channel = self.game_channel()
        if channel is None:
            return
        recent = [message async for message in channel.history(limit=20)]
        since = datetime.now(timezone.utc) - CRIER_QUIET_AFTER
        if not any(not message.author.bot and message.created_at > since for message in recent):
            return  # nobody has been around: a crier shouting into an empty square is just noise
        self.bot.store.set_state("crier:last", str(time.time()))
        today = datetime.now().date().isoformat()
        if self.bot.store.state("tarot:day") != today and random.random() < 0.4:
            self.bot.store.set_state("tarot:day", today)
            act(".", channel, "-", "the day's card")
            sent: list[discord.Message] = []

            async def send(view):
                sent.append(await channel.send(view=view, allowed_mentions=NO_PINGS))

            async def edit(view):
                await sent[0].edit(view=view, allowed_mentions=NO_PINGS)

            await tarot.deal(tarot.reading("today", channel.guild.id, ""), send, edit)  # nobody asked: no take
            return
        await channel.send(world.crier_line(), allowed_mentions=NO_PINGS)
        act(".", channel, "-", "the Crier spoke")

    # ── Right-click: contests, keks and Chuckles ──

    async def _both(self, interaction: discord.Interaction, member: discord.abc.User):
        mine = load_character(self.bot, interaction.guild_id, interaction.user.id)
        theirs = load_character(self.bot, interaction.guild_id, member.id)
        if member.id == interaction.user.id:
            await interaction.response.send_message("That's you.", ephemeral=True)
        elif mine is None:
            await interaction.response.send_message("Make a character first: `/character create`.", ephemeral=True)
        elif theirs is None:
            await interaction.response.send_message(f"{_who(member)} hasn't made a character yet.", ephemeral=True)
        else:
            return mine, theirs
        return None

    async def challenge(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if (pair := await self._both(interaction, member)) is not None:
            await interaction.response.send_message(f"Challenge **{pair[1].name}** to a contest of:", ephemeral=True,
                                                    view=ContestView(interaction.user, member, *pair))

    async def pay(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if await self._both(interaction, member) is not None:
            await interaction.response.send_modal(PayForm(self.bot, member))

    async def give_item(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if (pair := await self._both(interaction, member)) is None:
            return
        if not pair[0].inventory:
            await interaction.response.send_message(f"{pair[0].name} isn't carrying anything to hand over.", ephemeral=True)
            return
        await interaction.response.send_modal(GiveForm(self.bot, member, pair[0]))

    async def attack(self, interaction: discord.Interaction, member: discord.Member) -> None:
        """Right-click a member → Apps → Attack: your class weapon against their defence, damage taken off their HP.
        Only between characters in the same place on the map."""
        if (pair := await self._both(interaction, member)) is None:
            return
        mine, theirs = pair
        if mine.location != theirs.location:
            here, there = world.place(mine.location).name, world.place(theirs.location).name
            await interaction.response.send_message(f"{theirs.name} is at {there}; you're at {here}. Travel first.", ephemeral=True)
            return
        if mine.hp <= 0:
            await interaction.response.send_message(f"{mine.name} is down. Lie there and think about what you did.", ephemeral=True)
            return
        guild, attacker_id, who = interaction.guild_id, interaction.user.id, _who(interaction.user)

        async def resolve():
            me, them = load_character(self.bot, guild, attacker_id), load_character(self.bot, guild, member.id)
            result = combat.character_strike(me, them.name, combat.defence(them))
            dice_list = [(20, str(result.total), _tone(result.kept))]
            text = combat.render(result, who)
            if result.hit:
                dice_list.append((combat.die_sides(result.damage_dice), str(result.damage), "crit" if result.crit else "normal"))
                ledger.commit(self.bot.store, guild, [ledger.hp(member.id, -result.damage, f"{me.name}, {combat.weapon(me).name}")], "player")
                them = load_character(self.bot, guild, member.id)
                text += f"\n-# {them.name} · HP {them.hp}/{them.max_hp}" + (" · **down**" if them.hp <= 0 else "")
            return f"{member.mention} " + text, _picture(dice_list, "attack.png"), result.hit is False and me.chuckles > 0

        text, picture, retry = await resolve()
        view = RerollView(self.bot, guild, attacker_id, resolve) if retry else discord.utils.MISSING
        await interaction.response.send_message(text, file=picture, view=view, allowed_mentions=discord.AllowedMentions(users=[member]))
        act("^", interaction.channel, interaction.user, f"attacked {_who(member)[:20]}")

    async def card_for(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """Right-click a message → Apps → Draw a tarot card: one card for whoever wrote it, the message as the question."""
        question = message.clean_content if isinstance(message.clean_content, str) else ""
        act("^", interaction.channel, interaction.user, f"tarot for {_who(message.author)[:20]}")
        await answer_tarot(self.bot, interaction, tarot.reading("one", interaction.guild_id, _who(message.author), question))

    async def give_chuckle(self, interaction: discord.Interaction, message: discord.Message) -> None:
        """Right-click a message → Apps → Give a Chuckle: its author's character earns one (up to three)."""
        author = message.author
        character = None if author.bot else load_character(self.bot, interaction.guild_id, author.id)
        key = f"chuckle:{message.id}:{interaction.user.id}"
        if author.id == interaction.user.id:
            reply = "You can't chuckle at your own post. Well, you can, but it doesn't count."
        elif character is None:
            reply = "Only a member with a character can earn a Chuckle."
        elif self.bot.store.state(key):
            reply = "You already gave a Chuckle for that one."
        elif character.chuckles >= rules.MAX_CHUCKLES:
            reply = f"{character.name} is already holding all {rules.MAX_CHUCKLES}."
        else:
            ledger.commit(self.bot.store, interaction.guild_id, [ledger.chuckles(author.id, 1, f"from {_who(interaction.user)}")], "player")
            self.bot.store.set_state(key, "1")
            await interaction.response.send_message(f"-# {_who(interaction.user)} gave **{character.name}** a Chuckle for {message.jump_url}",
                                                    allowed_mentions=NO_PINGS)
            return
        await interaction.response.send_message(reply, ephemeral=True)

    # ── The map ──

    @world_group.command(name="map", description="The map of the Scrollands, and where everyone's characters are")
    async def world_map(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        people = []
        for user_id, data in self.bot.store.characters(interaction.guild_id):
            character = rules.Character.from_dict(data)
            portrait = None
            try:
                member = interaction.guild.get_member(user_id) or await interaction.guild.fetch_member(user_id)
                portrait = await member.display_avatar.replace(size=128, format="png").read()
            except (discord.HTTPException, AttributeError):
                pass
            people.append((character.name, character.location, portrait))
        listing: dict[str, list[str]] = {}
        for name, key, _portrait in people:
            listing.setdefault(world.place(key).name, []).append(name)
        summary = "\n".join(f"-# **{place}** · {', '.join(names)}" for place, names in listing.items()) or "-# Nobody has set out yet."
        clocks = ledger.world_state(self.bot.store, interaction.guild_id).get("clocks", {})
        if clocks:
            summary += "\n" + "\n".join(f"-# {ledger.clock_bar(clock)} {clock['label']}" for clock in clocks.values())
        await interaction.followup.send(summary, file=discord.File(io.BytesIO(world.map_png(people)), filename="scrollands.png"),
                                        allowed_mentions=NO_PINGS)

    @world_group.command(name="travel", description="Move your character to a place on the map")
    @app_commands.describe(place="Where to")
    async def travel(self, interaction: discord.Interaction, place: str) -> None:
        character = load_character(self.bot, interaction.guild_id, interaction.user.id)
        destination = world.PLACES.get(place) or world.find_place(place)
        if character is None or destination is None:
            await interaction.response.send_message("Make a character first: `/character create`." if character is None
                                                    else "Pick a place from the list.", ephemeral=True)
            return
        ledger.commit(self.bot.store, interaction.guild_id, [ledger.move(interaction.user.id, destination.key)], "player")
        await interaction.response.send_message(f"**{character.name}** sets out for **{destination.name}**.\n-# {destination.blurb}",
                                                allowed_mentions=NO_PINGS)

    @travel.autocomplete("place")
    async def travel_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        return [app_commands.Choice(name=p.name, value=p.key) for p in world.PLACES.values()
                if current.lower() in p.name.lower()][:25]

    @app_commands.command(name="roll", description="Roll dice (2d6+3, adv+5) or a check with your character (cope, mog adv)")
    @app_commands.describe(dice="Dice notation (2d6+3, 4d6kh3, adv+5) or a score (mog, cope, aura, lore)", reason="What it's for")
    async def roll(self, interaction: discord.Interaction, dice: str = "d20", reason: str = "") -> None:
        if dice.strip().lower() in ("help", "?"):
            await interaction.response.send_message(embed=dice_embed(self.bot), ephemeral=True)
            return
        notation = f"{dice} for {reason}" if reason.strip() else dice
        await send_roll(self.bot, interaction.response.send_message, interaction.guild_id, interaction.user.id, notation, _who(interaction.user))

    @roll.autocomplete("dice")
    async def roll_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        typed = current.strip()
        options = ([typed] if typed else []) + [option for option in ROLL_SUGGESTIONS if typed.lower() in option]
        return [app_commands.Choice(name=option, value=option) for option in dict.fromkeys(options)][:25]

    @character.command(name="create", description="Make your adventurer: name, class, faction, keepsake, then scores")
    async def create(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CreateForm(self))

    @app_commands.command(name="tarot", description="Mini Me reads the Rider–Waite–Smith deck: the day's card, one for you, or three")
    @app_commands.describe(reading="Which reading", question="What you're asking (optional)")
    @app_commands.choices(reading=[
        app_commands.Choice(name="The day's card (the same for everyone today)", value="today"),
        app_commands.Choice(name="One card for me", value="one"),
        app_commands.Choice(name="Three cards: what was, what is, what's coming", value="three"),
    ])
    async def tarot_command(self, interaction: discord.Interaction, reading: str = "today", question: str = "") -> None:
        act("^", interaction.channel, interaction.user, f"tarot {reading}")
        await answer_tarot(self.bot, interaction, tarot.reading(reading, interaction.guild_id, _who(interaction.user), question))

    @character.command(name="pack", description="Everything your character is carrying")
    async def pack(self, interaction: discord.Interaction) -> None:
        character = load_character(self.bot, interaction.guild_id, interaction.user.id)
        if character is None:
            await interaction.response.send_message("You don't have a character yet: `/character create`.", ephemeral=True)
            return
        lines = [f"-# *{character.keepsake}* (keepsake)"] if character.keepsake else []
        for item in sorted(character.inventory, key=lambda item: (item["kind"] != "quest", item["name"].casefold())):
            qty = f" ×{item['qty']}" if item["qty"] != 1 else ""
            note = f" · {item['note']}" if item.get("note") else ""
            lines.append(f"**{item['name']}**{qty} · {item['kind']}{note}")
        await interaction.response.send_message(f"**{character.name}** carries\n" + "\n".join(lines or ["Nothing at all."]),
                                                ephemeral=True, allowed_mentions=NO_PINGS)

    @character.command(name="log", description="What has happened to your character: keks, items, wounds, travel")
    async def log(self, interaction: discord.Interaction) -> None:
        character = load_character(self.bot, interaction.guild_id, interaction.user.id)
        if character is None:
            await interaction.response.send_message("You don't have a character yet: `/character create`.", ephemeral=True)
            return
        rows = self.bot.store.ledger(interaction.guild_id, interaction.user.id, limit=15)
        lines = [f"-# <t:{int(row['ts'])}:d> {ledger.describe(ledger.Entry.from_row(row), character.name)}" for row in rows]
        await interaction.response.send_message(f"**{character.name}**, lately\n" + "\n".join(lines or ["-# Nothing yet."]), ephemeral=True,
                                                allowed_mentions=NO_PINGS)

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
