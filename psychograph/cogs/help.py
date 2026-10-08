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
            "Right-click a message → Apps → **Review this debate** or **Judge this**",
        )
        + "\nReplying to a tool's answer goes back to this channel's persona. Tools need the MiMo model.",
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
        name="Duels and scores",
        value=_lines(
            "`/duel` *persona* *persona* *topic*: two personas argue in rounds, Jev judges · `/scores duels`",
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
            "`/bot model` · `/bot stats` · `/bot cost` · `/bot digest` · `/sound` · `/chess new`",
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
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            embed=help_embed(self.bot, interaction.channel_id, interaction.guild_id), ephemeral=True
        )


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(HelpCommands(bot))
