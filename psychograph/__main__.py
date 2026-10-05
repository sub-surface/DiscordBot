"""`python -m psychograph` runs the bot; `python -m psychograph stats [day|week|month|all]` prints reply stats."""

import sys

# Windows consoles default to cp1252, which can't print the emoji and symbols in logs and stats.
for stream in (sys.stdout, sys.stderr):
    if hasattr(stream, "reconfigure"):
        stream.reconfigure(encoding="utf-8", errors="replace")

if len(sys.argv) > 1 and sys.argv[1] == "stats":
    from .stats import main as stats_main

    stats_main(sys.argv[2] if len(sys.argv) > 2 else "week")
else:
    from .bot import main

    main()
