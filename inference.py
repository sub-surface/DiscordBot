from __future__ import annotations

import os
from collections.abc import AsyncIterator

from openai import AsyncOpenAI


async def stream(
    messages: list[dict],
    model: str,
    config: dict,
    max_tokens: int | None = None,
) -> AsyncIterator[str]:
    provider = config.get("providers", {}).get("local", {})
    client = AsyncOpenAI(
        base_url=os.getenv("LLM_BASE_URL", provider.get("base_url", "http://localhost:1234/v1")),
        api_key=os.getenv("LLM_API_KEY", provider.get("api_key", "lm-studio")),
    )
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=float(os.getenv("LLM_TEMPERATURE", config.get("response", {}).get("temperature", 0.7))),
            top_p=float(os.getenv("LLM_TOP_P", "0.95")),
            max_tokens=max_tokens or config.get("response", {}).get("max_tokens", 2048),
            extra_body={"top_k": int(os.getenv("LLM_TOP_K", "20"))},
            stream=True,
        )
        async for chunk in response:
            if not chunk.choices:
                continue
            text = chunk.choices[0].delta.content
            if text:
                yield text
    finally:
        await client.close()


async def complete_remote(
    messages: list[dict],
    model: str,
    max_tokens: int,
    temperature: float,
    top_p: float,
) -> str | dict[str, str | int | float | None]:
    import modal

    worker = modal.Cls.from_name("psychograph", "MimoWorker")
    return await worker().complete.remote.aio(
        messages,
        model,
        max_tokens,
        temperature,
        top_p,
    )
