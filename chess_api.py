"""
Stockfish move generation via chess-api.com (free, no auth).

Returns the best move for a given FEN position using Stockfish 18 NNUE.
"""

from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger("bot")

API_URL = "https://chess-api.com/v1"


async def get_stockfish_move(fen: str, depth: int = 12) -> dict | None:
    """
    Ask chess-api.com for the best move at *depth* for the given FEN.

    Returns the full response dict on success (keys: move, san, eval, …)
    or None on any failure.  Only the ``move`` (UCI) and ``san`` fields
    are needed by the caller.
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                API_URL,
                json={"fen": fen, "depth": depth},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    log.error("chess-api.com returned HTTP %d", resp.status)
                    return None
                data = await resp.json()
                if data.get("type") == "info":
                    # Error / status message, not a move
                    log.error("chess-api.com info: %s", data.get("text", data))
                    return None
                if "move" not in data:
                    log.error("chess-api.com response missing 'move': %s", data)
                    return None
                return data
    except Exception as e:
        log.error("chess-api.com request failed: %s", e)
        return None
