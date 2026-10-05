from __future__ import annotations

from psychograph.backends import Completion
from psychograph.bot import PsychographBot
from psychograph.settings import Settings
from psychograph.store import Store


class FakeBackend:
    name = "local"
    label = "test-model"
    note = "Test model."
    context_limit = 4096
    output_limit = 512

    def __init__(self, text: str = "A reply.") -> None:
        self.text = text
        self.calls: list[list[dict]] = []

    async def complete(self, messages: list[dict]) -> Completion:
        self.calls.append(messages)
        return Completion(text=self.text)

    async def close(self) -> None:
        return None


def make_bot(backend: FakeBackend | None = None) -> PsychographBot:
    return PsychographBot(Settings(), store=Store(":memory:"), backend=backend or FakeBackend())
