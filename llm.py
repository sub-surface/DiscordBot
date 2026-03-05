import asyncio
import base64
import json
import os
import urllib.request
import time
from typing import AsyncGenerator, Any

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

# Simple in-memory cache for models
_MODEL_CACHE = {}
MODEL_CACHE_TTL = 3600 # 1 hour

def get_client(provider: str, cfg: dict) -> AsyncOpenAI:
    if provider not in _clients:
        pcfg = cfg["providers"][provider]
        if provider == "local":
            _clients[provider] = AsyncOpenAI(
                base_url=pcfg["base_url"],
                api_key=pcfg.get("api_key", "lm-studio"),
            )
        elif provider == "openrouter":
            api_key = os.environ.get("OPENROUTER_API_KEY", "")
            if not api_key:
                raise ValueError("OPENROUTER_API_KEY is not set in .env")
            _clients[provider] = AsyncOpenAI(
                base_url=pcfg["base_url"],
                api_key=api_key,
                default_headers={
                    "HTTP-Referer": "https://github.com/sub-surface/discordbot",
                    "X-Title": "sub-surface/discordbot",
                },
            )
    return _clients[provider]

async def format_image_blocks(attachments) -> list[dict]:
    blocks = []
    for att in attachments:
        if not (att.content_type or "").startswith("image/"):
            continue
        try:
            data = await att.read()
            b64 = base64.b64encode(data).decode()
            blocks.append({
                "type": "image_url",
                "image_url": {"url": f"data:{att.content_type};base64,{b64}"},
            })
        except Exception:
            pass
    return blocks

async def get_local_models(cfg: dict) -> list[str]:
    try:
        client = get_client("local", cfg)
        models = await client.models.list()
        active = [m.id for m in models.data]
        if active: return active
    except Exception:
        pass
    return await asyncio.to_thread(_scan_disk_models)

def _scan_disk_models() -> list[str]:
    import glob
    path = "C:/Users/Leon/.cache/lm-studio/models/**/*.gguf"
    files = glob.glob(path, recursive=True)
    return sorted(list(set(os.path.basename(f) for f in files)))

async def get_openrouter_models(cfg: dict, free_only: bool = False, paid_only: bool = False) -> list[str]:
    cache_key = f"openrouter_{free_only}_{paid_only}"
    now = time.time()
    if cache_key in _MODEL_CACHE:
        models, expiry = _MODEL_CACHE[cache_key]
        if now < expiry:
            return models

    base_url = cfg["providers"]["openrouter"]["base_url"].rstrip("/")
    url = f"{base_url}/models"
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    def _fetch() -> dict:
        req = urllib.request.Request(url)
        if api_key: req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=10) as resp: return json.loads(resp.read())
    try:
        data = await asyncio.to_thread(_fetch)
        models = data.get("data", [])
        if free_only: models = [m for m in models if str(m.get("pricing", {}).get("prompt", "1")) == "0"]
        elif paid_only: models = [m for m in models if str(m.get("pricing", {}).get("prompt", "0")) != "0"]
        res = sorted(m["id"] for m in models)
        _MODEL_CACHE[cache_key] = (res, now + MODEL_CACHE_TTL)
        return res
    except Exception: return []

async def complete(
    messages: list[dict],
    provider: str,
    model: str,
    cfg: dict,
    temperature: float | None = None,
    max_tokens: int | None = None,
    use_tools: bool = True,
) -> AsyncGenerator[Any, None]:
    from search import web_search as do_web_search
    client = get_client(provider, cfg)
    resp_cfg = cfg.get("response", {})
    temp = temperature if temperature is not None else resp_cfg.get("temperature", 0.7)
    max_tok = max_tokens if max_tokens is not None else resp_cfg.get("max_tokens", 8192)
    start_time = time.time()
    tools_available = use_tools

    if tools_available:
        try:
            response = await client.chat.completions.create(
                model=model, messages=messages, max_tokens=max_tok, temperature=temp,
                tools=[WEB_SEARCH_TOOL], tool_choice="auto",
            )
        except BadRequestError: tools_available = False
        except Exception: raise

    if not tools_available:
        async for chunk in _stream(client, model, messages, temp, max_tok, start_time, provider):
            yield chunk
        return

    msg_obj = response.choices[0].message
    tool_calls = getattr(msg_obj, "tool_calls", None)

    if not tool_calls:
        yield (msg_obj.content or "", None)
        usage = getattr(response, "usage", None)
        if usage:
            yield (None, {"prompt_tokens": usage.prompt_tokens, "completion_tokens": usage.completion_tokens,
                          "duration": time.time() - start_time, "model": model, "provider": provider})
        return

    web_cfg = cfg.get("web_search", {})
    async def run_tool(tc) -> dict:
        try:
            args = json.loads(tc.function.arguments)
            if tc.function.name == "web_search": result = await do_web_search(args.get("query", ""), **web_cfg)
            else: result = f"Unknown tool: {tc.function.name}"
        except Exception as e: result = f"Tool error: {e}"
        return {"role": "tool", "tool_call_id": tc.id, "content": result}

    tool_results = await asyncio.gather(*[run_tool(tc) for tc in tool_calls])
    updated = messages + [{
        "role": "assistant", "content": msg_obj.content,
        "tool_calls": [{"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}} for tc in tool_calls],
    }] + list(tool_results)

    async for chunk in _stream(client, model, updated, temp, max_tok, start_time, provider, tool_choice="none"):
        yield chunk

async def _stream(client, model, messages, temp, max_tok, start_time, provider, tool_choice=None):
    kwargs = {"tool_choice": tool_choice} if tool_choice is not None else {}
    stream = await client.chat.completions.create(
        model=model, messages=messages, max_tokens=max_tok, temperature=temp,
        stream=True, stream_options={"include_usage": True}, **kwargs,
    )
    async for chunk in stream:
        if chunk.choices:
            delta = chunk.choices[0].delta.content
            if delta: yield (delta, None)
        if hasattr(chunk, "usage") and chunk.usage:
            yield (None, {"prompt_tokens": chunk.usage.prompt_tokens, "completion_tokens": chunk.usage.completion_tokens,
                          "duration": time.time() - start_time, "model": model, "provider": provider})

async def summarize(text: str, provider: str, model: str, cfg: dict) -> str:
    client = get_client(provider, cfg)
    prompt = f"Summarize the following conversation history concisely, retaining key facts, decisions, and context for future interactions:\n\n{text}\n\nSummary:"
    try:
        resp = await client.chat.completions.create(model=model, messages=[{"role": "user", "content": prompt}], max_tokens=500, temperature=0.3)
        return resp.choices[0].message.content.strip()
    except Exception as e: return f"Summary failed: {e}"
