import json
from pathlib import Path

from avatar_gen import generate_avatar

PERSONAS_DIR = Path(__file__).parent / "personas"

def get_persona_metadata(name: str) -> dict:
    """Get metadata for a persona including display name and avatar path."""
    path = PERSONAS_DIR / f"{name}.md"
    metadata = {"name": name, "avatar_path": generate_avatar(name)}
    
    if not path.exists():
        return metadata

    raw = path.read_text(encoding="utf-8").strip()
    try:
        data = json.loads(raw)
        if "name" in data:
            metadata["display_name"] = data["name"]
        if "avatar" in data:
            # If avatar is a URL, we use it directly. If it's a local path, we might need to handle it.
            metadata["avatar_url"] = data["avatar"]
    except (json.JSONDecodeError, KeyError):
        pass
    
    return metadata

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


def load_persona_style(name: str) -> dict | None:
    """Extract the 'style' dict from a JSON persona file, if present."""
    path = PERSONAS_DIR / f"{name}.md"
    if not path.exists():
        return None
    raw = path.read_text(encoding="utf-8").strip()
    try:
        data = json.loads(raw)
        return data.get("style")
    except (json.JSONDecodeError, KeyError):
        return None
