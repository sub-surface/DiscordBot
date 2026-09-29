from __future__ import annotations

from io import BytesIO

import discord
from discord import app_commands
from discord.ext import commands

import db
import chess_engine
from board import fen_to_image
from personas import load_persona


CHESS_PROMPT = (
    "\n\nYou are playing chess as Black. Choose exactly one legal move in SAN, "
    "then add one short sentence of commentary. Do not invent a move."
)


def _board_file(fen: str) -> discord.File | None:
    image = fen_to_image(fen)
    return discord.File(BytesIO(image), filename="board.png") if image else None


def board_embed(title: str, description: str, has_image: bool) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=0x347A68)
    if has_image:
        embed.set_image(url="attachment://board.png")
    return embed


async def send_board(target, title: str, description: str, file: discord.File | None) -> None:
    embed = board_embed(title, description, bool(file))
    if file:
        await target(embed=embed, file=file)
    else:
        await target(embed=embed)


async def play_move(bot: commands.Bot, channel: discord.abc.Messageable, move_text: str) -> tuple[str, discord.File | None]:
    ok, san, fen = chess_engine.apply_user_move(channel.id, move_text)
    if not ok:
        return san, None

    status = chess_engine.game_status(channel.id)
    if status:
        return status, _board_file(fen)

    system = (load_persona("chess") or "You are a concise chess opponent.") + CHESS_PROMPT
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": (
            f"The position is {fen}. Legal moves: {chess_engine.legal_moves_str(channel.id)}. "
            "Play your move."
        )},
    ]
    response = await bot.generate(messages)
    bot_move = chess_engine.extract_bot_move(response)
    if bot_move is None:
        return f"Your move was {san}. I couldn't find a legal move in my response; try again shortly.", _board_file(fen)

    ok, bot_san, fen = chess_engine.apply_bot_move(channel.id, bot_move)
    if not ok:
        return f"Your move was {san}. I proposed an illegal move ({bot_move}); the board is unchanged.", _board_file(fen)

    status = chess_engine.game_status(channel.id)
    summary = f"**{san}**  ·  **{bot_san}**"
    if status:
        summary += f"\n{status}"
    elif response:
        summary += f"\n{response[:500]}"
    return summary, _board_file(fen)


class ChessCommands(commands.Cog):
    chess = app_commands.Group(name="chess", description="Play a game of chess")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @chess.command(name="new", description="Start a new game in this channel")
    async def new_game(self, interaction: discord.Interaction) -> None:
        chess_engine.reset_game(interaction.channel_id)
        db.set_channel_persona(interaction.channel_id, "chess")
        fen = chess_engine.current_fen(interaction.channel_id)
        file = _board_file(fen)
        await send_board(
            interaction.response.send_message,
            "New game",
            "You are White. Send a move with `/chess move`.",
            file,
        )

    @chess.command(name="board", description="Show the current board")
    async def show_board(self, interaction: discord.Interaction) -> None:
        fen = chess_engine.current_fen(interaction.channel_id)
        file = _board_file(fen)
        await send_board(
            interaction.response.send_message,
            "Current position",
            f"Move {chess_engine.move_number(interaction.channel_id)}",
            file,
        )

    @chess.command(name="move", description="Play a move in SAN or UCI notation")
    @app_commands.describe(notation="For example: e4, Nf3, or e2e4")
    async def move(self, interaction: discord.Interaction, notation: str) -> None:
        await interaction.response.defer()
        summary, file = await play_move(self.bot, interaction.channel, notation)
        await send_board(interaction.followup.send, "Chess", summary, file)

    @chess.command(name="resign", description="Resign the current game")
    async def resign(self, interaction: discord.Interaction) -> None:
        fen = chess_engine.current_fen(interaction.channel_id)
        chess_engine.reset_game(interaction.channel_id)
        file = _board_file(fen)
        await send_board(
            interaction.response.send_message,
            "Game over",
            "You resigned. Start another with `/chess new`.",
            file,
        )
