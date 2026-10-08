from __future__ import annotations

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from psychograph.backends import Completion
from psychograph.bot import PsychographBot
from psychograph.profiles import DEFAULT_PROFILE, ModelProfile
from psychograph.settings import Settings
from psychograph.store import Store


class FakeBackend:
    name = "local"
    label = "test-model"
    note = "Test model."
    context_limit = 4096
    output_limit = 512
    temperature = 1.0
    top_p = 0.95

    def __init__(self, text: str = "A reply.", profile: ModelProfile = DEFAULT_PROFILE) -> None:
        self.text = text
        self.profile = profile
        self.calls: list[list[dict]] = []
        self.in_flight = 0
        self.likely_cold = False

    async def complete(self, messages: list[dict]) -> Completion:
        self.calls.append(messages)
        return Completion(text=self.text)

    async def close(self) -> None:
        return None


def make_bot(backend: FakeBackend | None = None) -> PsychographBot:
    return PsychographBot(Settings(), store=Store(":memory:"), backend=backend or FakeBackend())


class FakeDiscord:
    """Just enough of a channel and its messages to drive the chat pipeline."""

    def __init__(self, me: SimpleNamespace, channel_name: str = "sim-city") -> None:
        self.me = me
        self.ids = iter(range(2000, 3000))
        self.posted: list[SimpleNamespace] = []
        self.channel = SimpleNamespace(
            id=1,
            name=channel_name,
            parent=None,
            typing=nullcontext,
            send=AsyncMock(side_effect=self._post),
            fetch_message=AsyncMock(return_value=SimpleNamespace(author=me)),
            get_partial_message=lambda message_id: SimpleNamespace(delete=AsyncMock()),
            permissions_for=lambda member: SimpleNamespace(manage_messages=False),
        )

    async def _post(self, content=None, embed=None, **_kwargs) -> SimpleNamespace:
        message = SimpleNamespace(
            id=(message_id := next(self.ids)), content=content, embed=embed, add_reaction=AsyncMock(),
            remove_reaction=AsyncMock(), jump_url=f"https://discord.com/channels/10/1/{message_id}",
        )
        self.posted.append(message)
        return message

    def message(self, content: str, message_id: int, reference=None, mentioned: bool = True, author_id: int = 5):
        message = SimpleNamespace(
            id=message_id,
            author=SimpleNamespace(id=author_id, bot=False, name="leon", display_name="Leon"),
            content=content,
            mentions=[self.me] if mentioned else [],
            reference=reference,
            channel=self.channel,
            guild=SimpleNamespace(id=10),
            embeds=[],
            webhook_id=None,
            jump_url=f"https://discord.com/channels/10/1/{message_id}",
            add_reaction=AsyncMock(),
            remove_reaction=AsyncMock(),
        )
        message.reply = AsyncMock(side_effect=self._post)
        return message


def fake_webhooks(posted: list) -> MagicMock:
    webhooks = MagicMock()
    webhooks.available.return_value = True
    ids = iter(range(5000, 6000))

    async def send(channel, persona, content, allowed_mentions, file=None):
        message = SimpleNamespace(
            id=next(ids), content=content, persona=persona.name, file=file, add_reaction=AsyncMock()
        )
        posted.append(message)
        return message

    webhooks.send = AsyncMock(side_effect=send)
    webhooks.delete = AsyncMock(return_value=True)
    return webhooks
