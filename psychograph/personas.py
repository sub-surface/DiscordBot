"""Personas: built-in files, the chess mode, and per-server custom personas.

A persona is addressed by a key stored in channel settings:
  "<file stem>"   built-in persona from personas/*.json (structured) or *.md (plain prompt)
  "chess"         the chess mode — moves are played by Stockfish, not the language model
  "custom:<id>"   a custom persona from the store, visible only in its own server
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal
from urllib.parse import quote

from .store import Store

CUSTOM_PREFIX = "custom:"
CHESS_KEY = "chess"
DEFAULT_REACTION = "✨"
CUSTOM_REACTION = "🌱"
CUSTOM_PROMPT_LIMIT = 3500
COMPACT_PROMPT_LIMIT = 900
AVATAR_URL = "https://api.dicebear.com/9.x/notionists/png?seed={seed}&size=256&backgroundColor=d1f4e0,c0aede,ffdfbf,b6e3f4"


@dataclass(frozen=True)
class Persona:
    key: str
    name: str
    prompt: str
    reaction: str = DEFAULT_REACTION
    mode: Literal["chat", "chess"] = "chat"
    creator_id: int | None = None   # set for custom personas only
    guild_id: int | None = None
    compact_prompt: str = ""        # a short voice for weaker models; derived when not given
    avatar_url: str = ""

    def __post_init__(self) -> None:
        if not self.compact_prompt:
            object.__setattr__(self, "compact_prompt", compact_text(self.prompt))
        if not self.avatar_url:
            object.__setattr__(self, "avatar_url", AVATAR_URL.format(seed=quote(self.name)))

    @property
    def custom_id(self) -> int | None:
        return int(self.key.removeprefix(CUSTOM_PREFIX)) if self.key.startswith(CUSTOM_PREFIX) else None


def compact_text(text: str, limit: int = COMPACT_PROMPT_LIMIT) -> str:
    """The most voice-defining whole paragraphs of `text` that fit in `limit` characters.

    The opening paragraph (who the persona is) and any "Voice:"/"Style:"/"Tone:" paragraph
    come first, then the rest in order; the kept paragraphs stay in their original order.
    """
    text = text.strip()
    if len(text) <= limit:
        return text
    paragraphs = [paragraph.strip() for paragraph in re.split(r"\n\s*\n", text) if paragraph.strip()]
    is_voice = re.compile(r"^(?:\*\*)?(?:voice|style|tone)\b", re.IGNORECASE)
    ranked = sorted(range(len(paragraphs)), key=lambda index: (index != 0 and not is_voice.match(paragraphs[index]), index))
    chosen: list[int] = []
    used = 0
    for index in ranked:
        if used + len(paragraphs[index]) + 2 <= limit:
            chosen.append(index)
            used += len(paragraphs[index]) + 2
    if chosen:
        return "\n\n".join(paragraphs[index] for index in sorted(chosen))
    kept = ""
    sentences = re.split(r"(?<=[.!?])\s+", text)
    for sentence in sentences:
        if len(kept) + len(sentence) + 1 > limit:
            break
        kept = f"{kept} {sentence}".strip()
    return kept or text[:limit]


def split_compact_section(markdown: str) -> tuple[str, str]:
    """A Markdown persona may end with a "## Compact" section, used only in compact context mode."""
    match = re.search(r"^## Compact\s*$", markdown, flags=re.MULTILINE | re.IGNORECASE)
    if not match:
        return markdown.strip(), ""
    return markdown[: match.start()].strip(), markdown[match.end():].strip()


CHESS = Persona(key=CHESS_KEY, name="chess", prompt="", reaction="♟️", mode="chess")


def display_name(key: str) -> str:
    """How a built-in persona is named in Discord: its file stem, with underscores as spaces."""
    return key.replace("_", " ").strip()


def can_manage(persona: Persona, user_id: int, manage_guild: bool) -> bool:
    """Custom personas can be changed by their creator or anyone with Manage Server."""
    return persona.custom_id is not None and (user_id == persona.creator_id or manage_guild)


def render_structured(data: dict) -> str:
    """Flatten a structured persona into a system prompt: voice, then facts and state."""
    parts = [str(data.get("voice", "")).strip()]
    facts = data.get("facts") or {}
    if facts:
        lines = []
        for key, value in facts.items():
            if isinstance(value, list):
                value = ", ".join(str(item) for item in value) if value else "(none)"
            elif value is None:
                value = "(none)"
            lines.append(f"  {key}: {value}")
        parts.append("[Facts]\n" + "\n".join(lines))
    state = {key: value for key, value in (data.get("state") or {}).items() if value is not None}
    if state:
        parts.append("[Current state]\n" + "\n".join(f"  {key}: {value}" for key, value in state.items()))
    return "\n\n".join(part for part in parts if part)


def _custom(row: dict) -> Persona:
    return Persona(
        key=f"{CUSTOM_PREFIX}{row['id']}",
        name=row["name"],
        prompt=row["prompt"],
        reaction=CUSTOM_REACTION,
        creator_id=row["creator_id"],
        guild_id=row["guild_id"],
    )


class PersonaRegistry:
    def __init__(self, store: Store, personas_dir: Path, default_key: str) -> None:
        self.store = store
        self.personas_dir = personas_dir
        self.default_key = default_key

    def builtin_keys(self) -> list[str]:
        stems = {path.stem for pattern in ("*.json", "*.md") for path in self.personas_dir.glob(pattern)}
        return sorted(stems)

    def _builtin(self, key: str) -> Persona | None:
        structured = self.personas_dir / f"{key}.json"
        if structured.is_file():
            data = json.loads(structured.read_text(encoding="utf-8"))
            return Persona(
                key=key,
                name=display_name(key),
                prompt=render_structured(data),
                reaction=data.get("reaction", DEFAULT_REACTION),
                compact_prompt=data.get("compact") or compact_text(str(data.get("voice", ""))),
                avatar_url=data.get("avatar", ""),
            )
        plain = self.personas_dir / f"{key}.md"
        if plain.is_file():
            prompt, compact = split_compact_section(plain.read_text(encoding="utf-8"))
            return Persona(key=key, name=display_name(key), prompt=prompt, compact_prompt=compact)
        return None

    def _dressed(self, persona: Persona, guild_id: int | None, avatars: dict[str, str] | None = None) -> Persona:
        """Apply this server's uploaded avatar for the persona, if any."""
        if guild_id is None or persona.mode != "chat":
            return persona
        url = (avatars if avatars is not None else self.store.persona_avatars(guild_id)).get(persona.key)
        return replace(persona, avatar_url=url) if url else persona

    def get(self, guild_id: int | None, key: str) -> Persona | None:
        """The persona for an exact key, or None if it doesn't exist in this server."""
        if key == CHESS_KEY:
            return CHESS
        if key.startswith(CUSTOM_PREFIX):
            try:
                persona_id = int(key.removeprefix(CUSTOM_PREFIX))
            except ValueError:
                return None
            row = self.store.custom_persona(persona_id, guild_id) if guild_id is not None else None
            return self._dressed(_custom(row), guild_id) if row else None
        if "/" in key or "\\" in key or key not in self.builtin_keys():
            return None
        persona = self._builtin(key)
        return self._dressed(persona, guild_id) if persona else None

    def find(self, guild_id: int | None, text: str) -> Persona | None:
        """Look up by key, falling back to a case-insensitive name match (for typed input)."""
        persona = self.get(guild_id, text)
        if persona is not None:
            return persona
        wanted = text.strip().casefold()
        return next((persona for persona in self.available(guild_id) if persona.name.casefold() == wanted), None)

    def default(self) -> Persona:
        return self._builtin(self.default_key) or Persona(
            key=self.default_key, name=self.default_key, prompt=f"You are {self.default_key}."
        )

    def for_channel(self, channel_id: int, guild_id: int | None) -> Persona:
        key = self.store.channel_settings(channel_id).persona
        return (self.get(guild_id, key) if key else None) or self._dressed(self.default(), guild_id)

    def available(self, guild_id: int | None, custom_only: bool = False) -> list[Persona]:
        personas: list[Persona] = []
        if not custom_only:
            personas.extend(persona for key in self.builtin_keys() if (persona := self._builtin(key)))
            personas.append(CHESS)
        if guild_id is not None:
            personas.extend(_custom(row) for row in self.store.custom_personas(guild_id))
            avatars = self.store.persona_avatars(guild_id)
            personas = [self._dressed(persona, guild_id, avatars) for persona in personas]
        return personas

    def search(self, guild_id: int | None, query: str, custom_only: bool = False) -> list[Persona]:
        wanted = query.casefold()
        return [persona for persona in self.available(guild_id, custom_only) if wanted in persona.name.casefold()]

    def is_reserved(self, name: str) -> bool:
        reserved = {CHESS_KEY, *self.builtin_keys(), *(display_name(key) for key in self.builtin_keys())}
        return name.strip().casefold() in {item.casefold() for item in reserved}

    def delete(self, persona: Persona) -> bool:
        if persona.custom_id is None or persona.guild_id is None:
            return False
        return self.store.delete_custom_persona(persona.custom_id, persona.guild_id, persona.key, self.default_key)
