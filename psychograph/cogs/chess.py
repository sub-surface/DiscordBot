from __future__ import annotations

from io import BytesIO

import discord
from discord import app_commands
from discord.ext import commands

from ..board import fen_to_image
from ..bot import PsychographBot
from ..personas import CHESS_KEY
from ..render import board_embed


def board_file(fen: str) -> discord.File | None:
    image = fen_to_image(fen)
    return discord.File(BytesIO(image), filename="board.png") if image else None


async def send_board(send, title: str, description: str, fen: str | None) -> None:
    file = board_file(fen) if fen else None
    embed = board_embed(title, description, bool(file))
    await (send(embed=embed, file=file) if file else send(embed=embed))


class ChessCommands(commands.Cog):
    chess = app_commands.Group(name="chess", description="Play a game of chess")

    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot

    @chess.command(name="commentary", description="Enable or disable optional local LM commentary")
    @app_commands.describe(enabled="Comment on moves using local LM Studio; moves always use Stockfish")
    async def commentary(self, interaction: discord.Interaction, enabled: bool) -> None:
        self.bot.store.update_channel(interaction.channel_id, chess_commentary=enabled)
        state = "on (local LM Studio only)" if enabled else "off (CPU-only chess)"
        await interaction.response.send_message(f"Chess commentary is **{state}**.", ephemeral=True)

    @chess.command(name="new", description="Start a new game in this channel")
    async def new_game(self, interaction: discord.Interaction) -> None:
        self.bot.chess.reset(interaction.channel_id)
        self.bot.store.update_channel(interaction.channel_id, persona=CHESS_KEY)
        fen = self.bot.chess.board(interaction.channel_id).fen()
        await send_board(interaction.response.send_message, "New game", "You are White. Send a move with `/chess move`.", fen)

    @chess.command(name="board", description="Show the current board")
    async def show_board(self, interaction: discord.Interaction) -> None:
        board = self.bot.chess.board(interaction.channel_id)
        await send_board(interaction.response.send_message, "Current position", f"Move {board.fullmove_number}", board.fen())

    @chess.command(name="move", description="Play a move in SAN or UCI notation")
    @app_commands.describe(notation="For example: e4, Nf3, or e2e4")
    async def move(self, interaction: discord.Interaction, notation: str) -> None:
        await interaction.response.defer()
        turn = await self.bot.chess.play(interaction.channel_id, notation)
        await send_board(interaction.followup.send, "Chess", turn.summary, turn.fen)

    @chess.command(name="resign", description="Resign the current game")
    async def resign(self, interaction: discord.Interaction) -> None:
        fen = self.bot.chess.board(interaction.channel_id).fen()
        self.bot.chess.reset(interaction.channel_id)
        await send_board(interaction.response.send_message, "Game over", "You resigned. Start another with `/chess new`.", fen)


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(ChessCommands(bot))
