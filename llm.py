import asyncio
import base64
import json
import os
from typing import AsyncGenerator

from openai import AsyncOpenAI, BadRequestError

WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the web for current information. Use when you need up-to-date facts, "
            "news, or anything you cannot answer reliably from training data. "
            "Prefer answering directly when possible."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
            },
            "required": ["query"],
        },
    },
}

_clients: dict[str, AsyncOpenAI] = {}


def get_client(provider: str, cfg: dict) -> AsyncOpenAI:
    if provider not in _clients:
        pcfg = cfg["providers"][provider]
        if provider == "local":
            _clients[provider] = AsyncOpenAI(
                base_url=pcfg["base_url"],
                api_key=pcfg.get("api_key", "lm-studio"),
            )
        elif provider == "openrouter":
            _clients[provider] = AsyncOpenAI(
                base_url=pcfg["base_url"],
                api_key=os.environ.get("OPENROUTER_API_KEY", ""),
                default_headers={
                    "HTTP-Referer": "https://github.com/psychograph",
                    "X-Title": "Psychograph",
                },
            )
    return _clients[provider]


async def format_image_blocks(attachments) -> list[dict]:
    """Return OpenAI image_url content blocks for any image attachments."""
    blocks = []
    for att in attachments:
        if not (att.content_type or "").startswith("image/"):
            continue
        try:
            b64 = base64.b64encode(await att.read()).decode()
            blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{att.content_type};base64,{b64}"},
            })
        except Exception:
            pass
    return blocks


async def get_local_models(cfg: dict) -> list[str]:
    """Query LM Studio's /v1/models endpoint for loaded model IDs."""
    try:
        client = get_client("local", cfg)
        models = await client.models.list()
        return [m.id for m in models.data]
    except Exception:
        return []


async def complete(
    messages: list[dict],
    provider: str,
    model: str,
    cfg: dict,
    temperature: float | None = None,
    max_tokens: int | None = None,
    use_tools: bool = True,
) -> AsyncGenerator[str, None]:
    """
    Async generator yielding text chunks.

    Flow:
      - Pass 1 (non-streaming): detect tool calls
      - If tools called: execute web_search, then Pass 2 (streaming with tool results)
      - If no tools called: yield content directly
      - If model doesn't support tools (400): fall back to plain streaming
    """
    from search import web_search as do_web_search

    client = get_client(provider, cfg)
    resp_cfg = cfg.get("response", {})
    temp = temperature if temperature is not None else resp_cfg.get("temperature", 0.7)
    max_tok = max_tokens if max_tokens is not None else resp_cfg.get("max_tokens", 8192)

    tools_available = use_tools

    if tools_available:
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_tok,
                temperature=temp,
                tools=[WEB_SEARCH_TOOL],
                tool_choice="auto",
            )
        except BadRequestError:
            tools_available = False
        except Exception:
            raise

    if not tools_available:
        # Plain streaming — model doesn't support tool calling
        async for chunk in _stream(client, model, messages, temp, max_tok):
            yield chunk
        return

    msg_obj = response.choices[0].message
    tool_calls = getattr(msg_obj, "tool_calls", None)

    if not tool_calls:
        # No tools called — yield content directly (no need to stream again)
        yield msg_obj.content or ""
        return

    # Execute all tool calls in parallel
    web_cfg = cfg.get("web_search", {})

    async def run_tool(tc) -> dict:
        try:
            args = json.loads(tc.function.arguments)
            if tc.function.name == "web_search":
                result = await do_web_search(
                    args.get("query", ""),
                    max_results=web_cfg.get("max_results", 3),
                    snippet_chars=web_cfg.get("snippet_chars", 300),
                )
            else:
                result = f"Unknown tool: {tc.function.name}"
        except Exception as e:
            result = f"Tool error: {e}"
        return {"role": "tool", "tool_call_id": tc.id, "content": result}

    tool_results = await asyncio.gather(*[run_tool(tc) for tc in tool_calls])

    # Build updated messages with assistant tool-call message + results
    updated = messages + [
        {
            "role": "assistant",
            "content": msg_obj.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in tool_calls
            ],
        }
    ] + list(tool_results)

    # Pass 2: stream the final answer with tool results in context
    async for chunk in _stream(client, model, updated, temp, max_tok, tool_choice="none"):
        yield chunk


async def _stream(
    client: AsyncOpenAI,
    model: str,
    messages: list[dict],
    temperature: float,
    max_tokens: int,
    tool_choice: str | None = None,
):
    """Internal streaming helper. Yields text delta chunks."""
    kwargs = {"tool_choice": tool_choice} if tool_choice is not None else {}
    stream = await client.chat.completions.create(
        model=model,
        messages=messages,
        max_tokens=max_tokens,
        temperature=temperature,
        stream=True,
        **kwargs,
    )
    async for chunk in stream:
        if chunk.choices:
            delta = chunk.choices[0].delta.content
            if delta:
                yield delta
