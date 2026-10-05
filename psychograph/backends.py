"""Inference backends: a local LM Studio server or an on-demand Modal worker."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from openai import AsyncOpenAI

from .settings import Settings


@dataclass(frozen=True)
class Completion:
    text: str
    tokens: int | None = None             # None when the backend didn't report a count
    seconds: float | None = None          # backend-measured generation time, if reported
    tokens_per_second: float | None = None


class Backend(Protocol):
    name: str
    label: str           # the configured model target
    note: str            # what /model should say about the target
    context_limit: int
    output_limit: int

    async def complete(self, messages: list[dict]) -> Completion: ...
    async def close(self) -> None: ...


class LocalBackend:
    name = "local"
    note = "This is the configured LM Studio model."

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.label = settings.local_model
        self.context_limit = settings.local_context_tokens
        self.output_limit = settings.local_max_output_tokens
        self._client: AsyncOpenAI | None = None  # created on first use
        # LM Studio serves one generation at a time; queue here rather than at the server.
        self._lock = asyncio.Lock()

    async def complete(self, messages: list[dict]) -> Completion:
        if self._client is None:
            self._client = AsyncOpenAI(base_url=self.settings.local_base_url, api_key=self.settings.local_api_key)
        async with self._lock:
            result = await self._client.chat.completions.create(
                model=self.settings.local_model,
                messages=messages,
                temperature=self.settings.temperature,
                top_p=self.settings.top_p,
                max_tokens=self.output_limit,
                extra_body={"top_k": self.settings.top_k},
            )
        text = (result.choices[0].message.content or "").strip() if result.choices else ""
        tokens = result.usage.completion_tokens if result.usage else None
        return Completion(text=text, tokens=tokens if isinstance(tokens, int) and tokens > 0 else None)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()


class ModalBackend:
    name = "modal"
    note = (
        "This is the bot's configured target, not a live worker check. "
        "Model changes require dashboard selection and redeployment."
    )

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.label = f"{settings.modal_model_id}/{settings.modal_model_file}"
        self.context_limit = settings.modal_context_tokens
        self.output_limit = settings.modal_max_output_tokens

    async def complete(self, messages: list[dict]) -> Completion:
        # Imported lazily: on Windows, importing modal switches the default event loop
        # policy to one without subprocess support, which must not affect the bot's loop.
        import modal

        worker = modal.Cls.from_name(self.settings.modal_app, self.settings.modal_class)
        result = await worker().complete.remote.aio(
            messages,
            self.label,
            self.output_limit,
            self.settings.temperature,
            self.settings.top_p,
        )
        if not isinstance(result, dict):
            return Completion(text=str(result or "").strip())
        seconds = result.get("eval_seconds") or result.get("generation_seconds")
        tokens = result.get("completion_tokens")
        rate = result.get("tokens_per_second")
        return Completion(
            text=str(result.get("text") or "").strip(),
            tokens=tokens if isinstance(tokens, int) else None,
            seconds=float(seconds) if isinstance(seconds, (int, float)) else None,
            tokens_per_second=float(rate) if isinstance(rate, (int, float)) else None,
        )

    async def close(self) -> None:
        return None


def make_backend(settings: Settings) -> Backend:
    if settings.backend == "modal":
        return ModalBackend(settings)
    if settings.backend == "local":
        return LocalBackend(settings)
    raise ValueError(f"Unknown LLM_BACKEND {settings.backend!r}; use 'local' or 'modal'.")
