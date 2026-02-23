import json
from pathlib import Path

PERSONAS_DIR = Path(__file__).parent / "personas"


def list_personas() -> list[str]:
    return sorted(p.stem for p in PERSONAS_DIR.glob("*.md"))


def render_persona(data: dict) -> str:
    """Flatten a structured persona dict into a readable system prompt string."""
    parts = [data.get("voice", "").strip()]

    facts = data.get("facts", {})
    if facts:
        lines = []
        for k, v in facts.items():
            if isinstance(v, list):
                v = ", ".join(str(i) for i in v) if v else "(none)"
            elif v is None:
                v = "(none)"
            lines.append(f"  {k}: {v}")
        parts.append("[Facts]\n" + "\n".join(lines))

    return "\n\n".join(p for p in parts if p)


def load_persona(name: str) -> str | None:
    """Load and render a persona .md file. Returns None if file not found."""
    path = PERSONAS_DIR / f"{name}.md"
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    try:
        data = json.loads(raw)
        return render_persona(data)
    except (json.JSONDecodeError, KeyError):
        return raw  # plain-text persona fallback
