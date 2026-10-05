"""Inference backends: a local LM Studio server or an on-demand Modal worker."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Protocol

from openai import AsyncOpenAI

from .profiles import DEFAULT_PROFILE, ModelProfile, load_profiles, profile_for
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
    profile: ModelProfile
    context_limit: int
    output_limit: int
    temperature: float
    top_p: float
    in_flight: int

    @property
    def likely_cold(self) -> bool: ...
    async def complete(self, messages: list[dict]) -> Completion: ...
    async def close(self) -> None: ...


class _Backend:
    """Shared tuning: the model profile narrows the server's limits and sets sampling."""

    name = "base"
    note = ""

    def __init__(self, settings: Settings, label: str, server_context: int, output: int, profile: ModelProfile) -> None:
        self.settings = settings
        self.label = label
        self.profile = profile
        self.output_limit = profile.max_output_tokens or output
        budget = profile.prompt_budget
        self.context_limit = min(server_context, budget + self.output_limit) if budget else server_context
        self.temperature = profile.temperature if profile.temperature is not None else settings.temperature
        self.top_p = profile.top_p if profile.top_p is not None else settings.top_p
        self.in_flight = 0
        self._last_finished: float | None = None

    @property
    def likely_cold(self) -> bool:
        return False

    async def complete(self, messages: list[dict]) -> Completion:
        self.in_flight += 1
        try:
            return await self._complete(messages)
        finally:
            self.in_flight -= 1
            self._last_finished = time.monotonic()

    async def _complete(self, messages: list[dict]) -> Completion:
        raise NotImplementedError

    async def close(self) -> None:
        return None


class LocalBackend(_Backend):
    name = "local"
    note = "This is the configured LM Studio model."

    def __init__(self, settings: Settings, profile: ModelProfile = DEFAULT_PROFILE) -> None:
        super().__init__(
            settings, settings.local_model, settings.local_context_tokens, settings.local_max_output_tokens, profile
        )
        self._client: AsyncOpenAI | None = None  # created on first use
        # LM Studio serves one generation at a time; queue here rather than at the server.
        self._lock = asyncio.Lock()

    async def _complete(self, messages: list[dict]) -> Completion:
        if self._client is None:
            self._client = AsyncOpenAI(base_url=self.settings.local_base_url, api_key=self.settings.local_api_key)
        async with self._lock:
            result = await self._client.chat.completions.create(
                model=self.settings.local_model,
                messages=messages,
                temperature=self.temperature,
                top_p=self.top_p,
                max_tokens=self.output_limit,
                extra_body={"top_k": self.settings.top_k},
            )
        text = (result.choices[0].message.content or "").strip() if result.choices else ""
        tokens = result.usage.completion_tokens if result.usage else None
        return Completion(text=text, tokens=tokens if isinstance(tokens, int) and tokens > 0 else None)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()


class ModalBackend(_Backend):
    name = "modal"
    note = (
        "This is the bot's configured target, not a live worker check. "
        "Model changes require dashboard selection and redeployment."
    )

    def __init__(self, settings: Settings, profile: ModelProfile = DEFAULT_PROFILE) -> None:
        super().__init__(
            settings,
            f"{settings.modal_model_id}/{settings.modal_model_file}",
            settings.modal_context_tokens,
            settings.modal_max_output_tokens,
            profile,
        )

    @property
    def likely_cold(self) -> bool:
        """True when the worker has probably scaled to zero, so the next reply waits for a cold start."""
        if self.in_flight:
            return False
        idle_limit = self.settings.modal_scaledown_seconds + 5
        return self._last_finished is None or time.monotonic() - self._last_finished > idle_limit

    async def _complete(self, messages: list[dict]) -> Completion:
        # Imported lazily: on Windows, importing modal switches the default event loop
        # policy to one without subprocess support, which must not affect the bot's loop.
        import modal

        worker = modal.Cls.from_name(self.settings.modal_app, self.settings.modal_class)
        result = await worker().complete.remote.aio(
            messages, self.label, self.output_limit, self.temperature, self.top_p
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


def make_backend(settings: Settings) -> Backend:
    profiles = load_profiles(settings.models_file)
    if settings.backend == "modal":
        return ModalBackend(settings, profile_for(f"{settings.modal_model_id}/{settings.modal_model_file}", profiles))
    if settings.backend == "local":
        return LocalBackend(settings, profile_for(settings.local_model, profiles))
    raise ValueError(f"Unknown LLM_BACKEND {settings.backend!r}; use 'local' or 'modal'.")
