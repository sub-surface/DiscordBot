"""Reply statistics from the generation log, for /stats and `python -m psychograph stats`."""

from __future__ import annotations

PERIODS = {"day": "-1 day", "week": "-7 days", "month": "-30 days", "all": "-100 years"}


def stats_lines(stats: dict) -> list[str]:
    replies = stats["replies"] or 0
    if not replies:
        return ["No replies in this period."]

    def seconds(value: float | None) -> str:
        return f"{value:.1f}s" if value is not None else "—"

    ok = replies - stats["failures"]
    lines = [
        f"Replies: {replies} ({ok} ok, {stats['failures']} failed)",
        f"Average reply time: {seconds(stats['avg_seconds'])} · warm: {seconds(stats['avg_warm_seconds'])}",
        f"Cold starts: {stats['cold_starts']} · context trimmed: {stats['trimmed']}",
        f"Tokens generated: {stats['tokens']:,}"
        + (f" · {stats['avg_tokens_per_second']:.1f} tok/s" if stats["avg_tokens_per_second"] else ""),
    ]
    if stats["personas"]:
        lines.append("Top personas: " + ", ".join(f"{name} ({count})" for name, count in stats["personas"]))
    if stats["profiles"]:
        lines.append("Models: " + ", ".join(f"{name} ({count})" for name, count in stats["profiles"]))
    return lines


def main(period: str = "week") -> None:
    from .settings import load_settings
    from .store import Store

    settings = load_settings()
    store = Store(settings.db_path)
    try:
        print(f"Psychograph · last {period}")
        for line in stats_lines(store.generation_stats(PERIODS.get(period, PERIODS["week"]))):
            print(f"  {line}")
    finally:
        store.close()
