"""
Stockfish move generation via chess-api.com (free, no auth).

Returns the best move for a given FEN position using Stockfish 18 NNUE.
"""

from __future__ import annotations

import logging
import aiohttp

log = logging.getLogger("bot")

API_URL = "https://chess-api.com/v1"

# In-memory cache for chess moves to avoid redundant API hits
_CHESS_CACHE: dict[tuple[str, int], dict] = {}

async def get_stockfish_move(fen: str, depth: int = 12) -> dict | None:
    """
    Ask chess-api.com for the best move at *depth* for the given FEN.
    Results are cached to improve performance.
    """
    cache_key = (fen, depth)
    if cache_key in _CHESS_CACHE:
        return _CHESS_CACHE[cache_key]

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
                
                _CHESS_CACHE[cache_key] = data
                return data
    except Exception as e:
        log.error("chess-api.com request failed: %s", e)
        return None
