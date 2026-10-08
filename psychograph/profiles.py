"""Per-model tuning from models.json.

Stronger models (MiMo) get the full context: whole reply chains, structured persona
facts, and linked posts as JSON. Weaker or older models (MechaEpstein) get a compact
context: a short persona voice, the last few messages, and plainly worded turns,
which keeps them in character and stops them drifting.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class ModelProfile:
    key: str = "default"
    name: str = "Default"
    match: tuple[str, ...] = ()
    context_mode: Literal["full", "compact"] = "full"
    history_messages: int = 40
    channel_messages: int = 0               # recent channel messages chat personas also see
    prompt_budget: int | None = None        # cap on input tokens, below the server's context
    max_output_tokens: int | None = None    # None → the backend's configured limit
    temperature: float | None = None
    top_p: float | None = None
    tools: bool = False                     # strong enough for tool personas (debate review, judge…)
    modal: dict = field(default_factory=dict, compare=False)

    @property
    def compact(self) -> bool:
        return self.context_mode == "compact"

    def matches(self, model: str) -> bool:
        model = model.casefold()
        return any(token.casefold() in model for token in self.match)

    def describe(self) -> str:
        budget = f", ≤{self.prompt_budget:,} prompt tokens" if self.prompt_budget else ""
        tools = " · tools on" if self.tools else " · tools off"
        channel = f" + {self.channel_messages} channel" if self.channel_messages else ""
        return f"{self.name} · {self.context_mode} context ({self.history_messages} msgs{channel}{budget}){tools}"


DEFAULT_PROFILE = ModelProfile()


def load_profiles(path: Path) -> list[ModelProfile]:
    if not path.is_file():
        return []
    profiles = []
    for entry in json.loads(path.read_text(encoding="utf-8")).get("models", []):
        chat = entry.get("chat", {})
        profiles.append(
            ModelProfile(
                key=entry["key"],
                name=entry.get("name", entry["key"]),
                match=tuple(entry.get("match", [entry["key"]])),
                context_mode=chat.get("context_mode", "full"),
                history_messages=int(chat.get("history_messages", 40)),
                channel_messages=int(chat.get("channel_messages", 0)),
                prompt_budget=chat.get("prompt_budget"),
                max_output_tokens=chat.get("max_output_tokens"),
                temperature=chat.get("temperature"),
                top_p=chat.get("top_p"),
                tools=bool(chat.get("tools", False)),
                modal=entry.get("modal", {}),
            )
        )
    return profiles


def profile_for(model: str, profiles: list[ModelProfile]) -> ModelProfile:
    return next((profile for profile in profiles if profile.matches(model)), DEFAULT_PROFILE)
