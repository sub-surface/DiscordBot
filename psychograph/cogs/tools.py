"""Tool features that need no model: the debate leaderboard and the predictions ledger.

React 🔮 on a message to log it as a prediction; /scores predictions lists the open ones and lets
anyone but the predictor settle them as right, wrong or void. /scores debates ranks members by debate
reviews won.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import PsychographBot, is_allowed_channel
from ..conversation import clip
from ..render import CHOICE_LIMIT, EMBED_COLOR

PREDICT, LOGGED = "🔮", "📌"
OUTCOME_ICONS = {"right": "✅", "wrong": "❌", "void": "🚫"}
SHOWN = 10


def _when(message_id: int) -> str:
    return f"<t:{int(discord.utils.snowflake_time(message_id).timestamp())}:R>"


def debates_embed(bot: PsychographBot, guild_id: int) -> discord.Embed:
    table = bot.store.debate_table(guild_id)
    embed = discord.Embed(title="Debate leaderboard", color=EMBED_COLOR)
    if not table:
        embed.description = "No debates reviewed yet. After an argument, mention me with **debate review** or right-click its last message → **Apps → Review this debate**."
        return embed
    lines = [
        f"`{index + 1:>2}.` <@{row['user_id']}>: **{row['wins']}**W {row['losses']}L"
        + (f" {row['splits']} split" if row["splits"] else "")
        for index, row in enumerate(table[:15])
    ]
    embed.description = "\n".join(lines)
    embed.set_footer(text="Each review counts once; asking again about the same people within 6 hours replaces it.")
    return embed


def duels_embed(bot: PsychographBot, guild_id: int) -> discord.Embed:
    table = bot.store.duel_table(guild_id)
    embed = discord.Embed(title="Duel records", color=EMBED_COLOR)
    if not table:
        embed.description = "No duels yet. Start one with `/duel`."
        return embed

    def name(key: str) -> str:
        persona = bot.personas.get(guild_id, key)
        return persona.name if persona else key

    embed.description = "\n".join(
        f"`{index + 1:>2}.` **{name(row['persona'])}**: {row['wins']}W {row['losses']}L"
        + (f" {row['draws']}D" if row["draws"] else "")
        for index, row in enumerate(table[:15])
    )
    embed.set_footer(text="Judged by Jev after every /duel.")
    return embed


def predictions_embed(bot: PsychographBot, guild_id: int) -> discord.Embed:
    store = bot.store
    open_items = store.predictions(guild_id, "open", limit=SHOWN)
    embed = discord.Embed(title="Predictions", color=EMBED_COLOR)
    if open_items:
        embed.description = "\n".join(
            f"<@{item['author_id']}> {_when(item['message_id'])}: [{clip(item['text'], 120)}]"
            f"(https://discord.com/channels/{guild_id}/{item['channel_id']}/{item['message_id']})"
            for item in open_items
        )
    else:
        embed.description = f"Nothing open. React {PREDICT} on a message to log it as a prediction."
    record = [row for row in store.prediction_table(guild_id) if row["right"] or row["wrong"]]
    if record:
        embed.add_field(
            name="Track record",
            value="\n".join(f"<@{row['author_id']}> ✅ {row['right']} · ❌ {row['wrong']}" for row in record[:10]),
            inline=False,
        )
    embed.set_footer(text="Pick one below to settle it. Anyone but the predictor can.")
    return embed


class SettleView(discord.ui.View):
    """Right / wrong / void for one prediction."""

    def __init__(self, bot: PsychographBot, message_id: int) -> None:
        super().__init__(timeout=120)
        self.bot = bot
        self.message_id = message_id
        for outcome, style in (("right", discord.ButtonStyle.success), ("wrong", discord.ButtonStyle.danger), ("void", discord.ButtonStyle.secondary)):
            button = discord.ui.Button(label=outcome.title(), emoji=OUTCOME_ICONS[outcome], style=style)
            button.callback = self._settle(outcome)
            self.add_item(button)

    def _settle(self, outcome: str):
        async def callback(interaction: discord.Interaction) -> None:
            self.stop()
            prediction = self.bot.store.prediction(self.message_id)
            if prediction is None or not self.bot.store.resolve_prediction(self.message_id, outcome, interaction.user.id):
                await interaction.response.edit_message(content="That prediction is already settled.", view=None)
                return
            await interaction.response.edit_message(content=f"Settled as {OUTCOME_ICONS[outcome]} **{outcome}**.", view=None)
            if interaction.channel is not None:
                await interaction.channel.send(
                    f"{OUTCOME_ICONS[outcome]} <@{prediction['author_id']}>'s prediction is settled **{outcome}** "
                    f"by {interaction.user.mention}: “{clip(prediction['text'], 200)}”",
                    allowed_mentions=discord.AllowedMentions.none(),
                )

        return callback


class PredictionsView(discord.ui.View):
    def __init__(self, bot: PsychographBot, guild_id: int) -> None:
        super().__init__(timeout=600)
        self.bot = bot
        items = bot.store.predictions(guild_id, "open", limit=CHOICE_LIMIT)
        if not items:
            return
        select = discord.ui.Select(
            placeholder="Settle a prediction…",
            options=[
                discord.SelectOption(label=clip(item["text"], 100), value=str(item["message_id"]))
                for item in items
            ],
        )
        select.callback = self._chosen
        self.select = select
        self.add_item(select)

    async def _chosen(self, interaction: discord.Interaction) -> None:
        message_id = int(self.select.values[0])
        prediction = self.bot.store.prediction(message_id)
        if prediction is None or prediction["status"] != "open":
            await interaction.response.send_message("That prediction is already settled.", ephemeral=True)
            return
        moderator = bool(interaction.permissions and interaction.permissions.manage_messages)
        if prediction["author_id"] == interaction.user.id and not moderator:
            await interaction.response.send_message("You can't settle your own prediction — ask someone else.", ephemeral=True)
            return
        await interaction.response.send_message(
            f"“{clip(prediction['text'], 300)}” — <@{prediction['author_id']}>. How did it go?",
            view=SettleView(self.bot, message_id),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class ToolCommands(commands.Cog):
    scores = app_commands.Group(name="scores", description="Leaderboards: debates won and predictions called")

    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot

    @scores.command(name="debates", description="The debate leaderboard: wins from debate reviews")
    async def debates(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("The leaderboard lives in a server.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=debates_embed(self.bot, interaction.guild_id), allowed_mentions=discord.AllowedMentions.none()
        )

    @scores.command(name="duels", description="Persona duel records: wins, losses and draws")
    async def duels(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Duels live in a server.", ephemeral=True)
            return
        await interaction.response.send_message(embed=duels_embed(self.bot, interaction.guild_id))

    @scores.command(name="predictions", description="Open predictions to settle, and everyone's track record")
    async def predictions(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message("Predictions live in a server.", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=predictions_embed(self.bot, interaction.guild_id),
            view=PredictionsView(self.bot, interaction.guild_id),
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        """🔮 on a member's message logs it as a prediction."""
        if str(payload.emoji) != PREDICT or payload.guild_id is None or self.bot.user is None:
            return
        if payload.user_id == self.bot.user.id or self.bot.store.prediction(payload.message_id) is not None:
            return
        if self.bot.ignoring(payload.guild_id, payload.user_id) is not None:
            return
        channel = self.bot.get_channel(payload.channel_id)
        if channel is None or not is_allowed_channel(channel, self.bot.settings.allowed_channels):
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.HTTPException:
            return
        text = message.clean_content.strip()
        if message.author.bot or message.webhook_id or not text:
            return
        if self.bot.store.add_prediction(
            message.id, payload.guild_id, channel.id, message.author.id, text[:500], payload.user_id
        ):
            try:
                await message.add_reaction(LOGGED)
            except discord.HTTPException:
                pass


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(ToolCommands(bot))
