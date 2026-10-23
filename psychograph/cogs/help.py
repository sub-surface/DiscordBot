"""/help, plus the cards the bot answers with instantly when asked about itself (see jev.DIRECT)."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import PsychographBot
from ..render import EMBED_COLOR, model_summary

BOT_NAME = "Mecha Epstein"  # what members know the bot as; "Psychograph" is only the code's name


def _lines(*lines: str) -> str:
    return "\n".join(f"- {line}" for line in lines)


def dice_embed(bot: PsychographBot) -> discord.Embed:
    """The dice cheat sheet: notation in plain words, then the rolls people actually make."""
    me = getattr(bot.user, "mention", None) or f"@{BOT_NAME}"
    embed = discord.Embed(
        title="Dice cheat sheet",
        description=(
            "`XdY` means *roll X dice with Y sides and add them up*. So `d20` is one twenty-sided die, "
            "`3d6` is three six-sided dice added together, and `+2` or `-1` adds or takes away a flat number."
        ),
        color=EMBED_COLOR,
    )
    embed.add_field(name="Everything you can type", value=_lines(
        "`d20` · `2d6+3` · `1d8+1d6+2`: dice and modifiers, mixed freely",
        "`adv` / `dis`: advantage or disadvantage, a d20 rolled twice keeping the higher / lower (`adv+5` works)",
        "`4d6kh3`: roll 4d6, **k**eep the **h**ighest 3 · `kl` keeps the lowest · `dl1` / `dh1` **d**rop the lowest / highest",
        "`d6!`: exploding, a die that rolls its max rolls again and adds",
        "`d%`: percentile, 1 to 100",
        "`6x 4d6kh3`: the same roll six times (a full set of stats)",
        "`… for stealth` or `# stealth`: the reason, shown with the result",
    ), inline=False)
    embed.add_field(name="Rolls you'll actually make", value=_lines(
        "A check or an attack: `d20+5` (your bonus after the +)",
        "Damage: `2d6+3` · a fireball: `8d6`",
        "Rolling a character: `6x 4d6kh3`",
        "Initiative: `d20+2` · a coin flip: `d2`",
    ), inline=False)
    embed.add_field(name="Where", value=_lines(
        "`/roll` (pick from the suggestions as you type)",
        "a message that's only a roll: **roll d20 for stealth**",
        f"{me} roll for initiative (no dice means a d20)",
    ) + "\nA lone d20 that lands on 20 or 1 is a *natural 20* or *natural 1*, and you'll see it.", inline=False)
    embed.set_footer(text="Instant · rolled locally · no model call")
    return embed


def game_embed(bot: PsychographBot) -> discord.Embed:
    """The Scrollands at a glance: making a character, checks, the map and the right-click actions."""
    embed = discord.Embed(
        title="The Scrollands",
        description=("A dark, very online fantasy world played in #" + bot.settings.game_channel + ". Make an adventurer, "
                     "roll checks with your scores, and wander a realm built out of the server's memes."),
        color=EMBED_COLOR,
    )
    embed.add_field(name="Your character", value=_lines(
        "`/character create`: name, class (each plays like a D&D class), faction, keepsake, then your scores",
        "`/character sheet`, or right-click a member → Apps → **Character sheet**",
        "Scores: **MOG** body · **COPE** nerve and argument · **AURA** presence · **LORE** knowing things",
    ), inline=False)
    embed.add_field(name="Checks", value=_lines(
        "`roll perception`, `roll stealth for the guards`, `roll cope adv`, `save mog`, `roll initiative`",
        "1–9 it goes wrong · 10–14 yes, but · 15+ yes · natural 20 mogged · natural 1 spawnpilled",
        "A roll at the end of a roleplay message counts too: *…I pick the lock. Roll sleight*",
        "A bad roll offers **Spend a Chuckle · reroll**: one click, the new roll stands",
    ), inline=False)
    embed.add_field(name="Fighting", value=_lines(
        "`roll attack` (or `roll attack vs 13`): your class weapon, d20 + bonus against a defence",
        "A hit rolls damage; a natural 20 doubles the dice · `roll damage` for damage alone",
        "Right-click a member in the same place → **Attack**: it comes off their HP",
    ), inline=False)
    embed.add_field(name="The world", value=_lines(
        "`/world map`: the realm and where everyone is · `/world travel`: set out somewhere",
        "Right-click a member → **Challenge to a contest** · **Pay keks** · **Hand over an item**",
        "`/character pack`: everything you carry · `/character log`: what has happened to you lately",
        "`/tarot`: Mini Me reads the deck: the day's card (the same for everyone), one card, or three",
        "Or just type `draw a card`, `three cards`, `tarot: will I win` · right-click a post → **Draw a tarot card**",
        "Right-click a great post → **Give a Chuckle** (spend Chuckles for advantage or a reroll)",
    ), inline=False)
    embed.set_footer(text="Dice and sheets are instant · /help dice for the notation")
    return embed


def help_embed(bot: PsychographBot, channel_id: int, guild_id: int | None) -> discord.Embed:
    """The /help card: what to type, grouped by what answers it (the model, tools, or instantly)."""
    persona = bot.personas.for_channel(channel_id, guild_id)
    me = getattr(bot.user, "mention", None) or f"@{BOT_NAME}"
    embed = discord.Embed(
        title=BOT_NAME,
        description=(
            f"A chat bot with many personas. This channel is talking to **{persona.name}**.\n"
            "Mention me or reply to one of my messages. Ask in plain words and I'll work out whether you want "
            "a chat, a tool or a quick answer, or start with a tool's name to be sure."
        ),
        color=EMBED_COLOR,
    )
    embed.set_thumbnail(url=persona.avatar_url)
    embed.add_field(
        name="Chat (the model)",
        value=_lines(
            f"{me} *anything*: talk to this channel's persona, who sees what the chat is about",
            "Or call one by name: *aura-bot thoughts?* for a chatter, *mochi, thoughts?* for a character",
            "`/ask` *persona* *prompt*: a one-off answer from any persona",
            "Right-click a message → Apps → **Ask persona**: the persona responds to it",
            f"{me} tell @someone *…*: answers and pings only them · x.com links get read",
        ),
        inline=False,
    )
    embed.add_field(
        name="Tools (checked instantly, written by the model)",
        value=_lines(
            f"{me} debate review: who won the argument, with a ruling",
            f"{me} judge: is it wrong to *…*: a moral verdict",
            f"{me} minutes: what you missed",
            f"{me} steelman both sides: the best case for each",
            "Right-click a message → Apps → **Review this debate**",
        )
        + "\nReplying to a tool's answer goes back to this channel's persona. Tools need a strong model (MiniCPM-V, MiMo).",
        inline=False,
    )
    embed.add_field(
        name="Quick (instant, no model)",
        value=_lines(
            "`/quick decide` pizza or curry · `/quick odds` will it rain · `/quick tier` crocs, tea, jury duty",
            "`/quick rate` a take · `/quick tone` a message · `/quick vibe` · `/quick chatter`",
            f"Or {me} decide: *…*, odds: *…*, vibe check, or reply to a message with tone check",
            "Right-click a message → Apps → **Tone check**",
            "Ask me what I can do, which persona this is, or what model I'm on",
        ),
        inline=False,
    )
    embed.add_field(
        name="Party (instant, no model)",
        value=_lines(
            f"`/roll` *2d6+3* · or just say **roll d20 for stealth** · or {me} roll for initiative",
            "`adv+5` / `dis` · `4d6kh3` keep highest · `d6!` exploding · `6x 4d6kh3` stats · **`/help dice`** explains it all",
            "**The Scrollands**, the server's tabletop game: `/character create` · `/world map` · **`/help game`**",
        ),
        inline=False,
    )
    embed.add_field(
        name="Duels and scores",
        value=_lines(
            "`/duel` *persona* *persona* *topic*: two personas argue in rounds, Jev judges · `/scores duels`",
            "`/santi-slop` *day|week|month|all*: the Slop Report, the most viral posts shared in #sim-city",
            "`/scores debates`: the debate leaderboard",
            "React 🔮 on a message to log it as a prediction · `/scores predictions` to settle them",
        )
        + "\nOn my own I sometimes react to a message that lands, drop into a live chat or pick up a question "
        "nobody answered, and post the week's digest on Mondays.",
        inline=False,
    )
    embed.add_field(
        name="Personas and settings",
        value=_lines(
            "`/persona` *name*: switch this channel's persona · `/persona-manage`: create, edit or picture one",
            "`/status`: channel settings and controls · `/verbosity` · `/reset`",
            "`/timeout` *member* *30m*: I ignore them for a while (moderators)",
            "`/bot model` · `/bot stats` · `/bot cost` · `/bot thinking` · `/bot tarot` · `/bot digest` · `/sound` · `/chess new`",
        ),
        inline=False,
    )
    embed.add_field(
        name="Reactions",
        value=_lines(
            "On my answers: 🔁 regenerate (whoever asked) · 🗑️ delete (whoever asked, or moderators)",
            "While I work: 👀 thinking · ☁️ waking the GPU (30–60 s) · ⏳ queued",
        ),
        inline=False,
    )
    embed.set_footer(text=f"{bot.backend.profile.describe()} · /bot stats")
    return embed


def personas_embed(bot: PsychographBot, channel_id: int, guild_id: int | None) -> discord.Embed:
    """This channel's persona and every persona available here, by group."""
    current = bot.personas.for_channel(channel_id, guild_id)
    embed = discord.Embed(
        title=f"This channel is {current.name}",
        description="\n".join(filter(None, [
            current.about,
            "Switch with `/persona` · make your own with `/persona-manage` · "
            "call a tool from anywhere by name (judge, minutes, steelman, debate review)",
        ])),
        color=EMBED_COLOR,
    )
    embed.set_thumbnail(url=current.avatar_url)
    groups: dict[str, list[str]] = {}
    for persona in bot.personas.available(guild_id):
        groups.setdefault(persona.group, []).append(persona.name)
    for group, names in groups.items():
        embed.add_field(name=group.title(), value=", ".join(names)[:1024], inline=False)
    return embed


def model_embed(bot: PsychographBot) -> discord.Embed:
    return discord.Embed(title="What I'm running on", description=model_summary(bot.backend), color=EMBED_COLOR)

class HelpCommands(commands.Cog):
    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="What I can do and how to use it")
    @app_commands.describe(topic="A cheat sheet for one feature")
    @app_commands.choices(topic=[app_commands.Choice(name="dice", value="dice"), app_commands.Choice(name="game", value="game")])
    async def help(self, interaction: discord.Interaction, topic: str = "") -> None:
        embed = (dice_embed(self.bot) if topic == "dice" else game_embed(self.bot) if topic == "game"
                 else help_embed(self.bot, interaction.channel_id, interaction.guild_id))
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(HelpCommands(bot))
