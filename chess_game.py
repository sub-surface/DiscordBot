from __future__ import annotations

import asyncio
import logging
from io import BytesIO

import discord
from discord import app_commands
from discord.ext import commands

import db
import chess_engine
from board import fen_to_image
from personas import load_persona


CHESS_COMMENTARY_PROMPT = (
    "You are commenting on a completed chess turn. Do not suggest or invent moves. "
    "Give one brief, accurate observation about the position or last move."
)
log = logging.getLogger("psychograph.chess")


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

    try:
        bot_move = await asyncio.to_thread(chess_engine.find_cpu_move, channel.id)
    except Exception:
        chess_engine.undo_last_move(channel.id)
        log.exception("CPU chess engine failed in channel %s", channel.id)
        return (
            "I couldn't get a move from the local chess engine, so I rolled back yours. Check that Stockfish is installed and STOCKFISH_PATH is set.",
            _board_file(chess_engine.current_fen(channel.id)),
        )

    ok, bot_san, fen = chess_engine.apply_bot_move(channel.id, bot_move)
    if not ok:
        chess_engine.undo_last_move(channel.id)
        log.error("Stockfish returned an illegal move in channel %s: %s", channel.id, bot_move)
        return "The local chess engine returned an invalid move, so I rolled yours back.", _board_file(
            chess_engine.current_fen(channel.id)
        )

    status = chess_engine.game_status(channel.id)
    summary = f"**{san}**  ·  **{bot_san}**"
    if status:
        summary += f"\n{status}"
    elif db.get_chess_commentary(channel.id):
        messages = [
            {"role": "system", "content": CHESS_COMMENTARY_PROMPT},
            {"role": "user", "content": f"The moves were {san} and {bot_san}. Current position: {fen}."},
        ]
        try:
            commentary = await bot.generate_local(messages)
        except Exception:
            log.exception("Optional local chess commentary failed in channel %s", channel.id)
        else:
            if commentary:
                summary += f"\n{commentary[:500]}"
    return summary, _board_file(fen)


class ChessCommands(commands.Cog):
    chess = app_commands.Group(name="chess", description="Play a game of chess")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @chess.command(name="commentary", description="Enable or disable optional local LM commentary")
    @app_commands.describe(enabled="Comment on moves using local LM Studio; moves always use Stockfish")
    async def commentary(self, interaction: discord.Interaction, enabled: bool) -> None:
        db.set_chess_commentary(interaction.channel_id, enabled)
        state = "on (local LM Studio only)" if enabled else "off (CPU-only chess)"
        await interaction.response.send_message(f"Chess commentary is **{state}**.", ephemeral=True)

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
