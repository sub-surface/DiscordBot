from __future__ import annotations

import json
import sys

import chess
import chess.engine


def main() -> None:
    request = json.load(sys.stdin)
    board = chess.Board(request["fen"])
    with chess.engine.SimpleEngine.popen_uci(request["executable"]) as engine:
        options = {}
        if "Threads" in engine.options:
            options["Threads"] = request["threads"]
        if "Hash" in engine.options:
            options["Hash"] = request["hash_mb"]
        if options:
            engine.configure(options)
        result = engine.play(board, chess.engine.Limit(time=request["move_time"]))
    if result.move is None:
        raise RuntimeError("Stockfish did not return a move.")
    print(result.move.uci())


if __name__ == "__main__":
    main()