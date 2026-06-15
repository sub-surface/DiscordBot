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

SIM_CITY_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "summon_persona",
            "description": "Bring another persona into this conversation thread. They will respond after you.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "The persona slug to summon (e.g. 'vostok', 'plateau')"}
                },
                "required": ["name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_scene",
            "description": "Update the current sim-city topic/scene. All future responses in this channel will be informed by this context.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "The new scene or topic for sim-city"}
                },
                "required": ["topic"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_persona",
            "description": (
                "Create or overwrite a persona file. Use this to introduce a new character into sim-city. "
                "Schema: {\"name\": \"slug\", \"voice\": \"first-person 150-300 word monologue capturing register, "
                "worldview, behavioural rules — the load-bearing field\", "
                "\"facts\": {\"key\": \"value — max 8 fields, only what makes them specific\"}, "
                "\"state\": {\"mutable_field\": null}, "
                "\"style\": {\"color\": \"0xHEXCODE\", \"footer\": \" · tag · \"}}. "
                "Voice must be dense and specific. No filler biography. State fields are nullable and updated mid-conversation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Persona slug (lowercase, underscores)"},
                    "json_content": {"type": "string", "description": "Full persona JSON as a string"}
                },
                "required": ["name", "json_content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_persona",
            "description": "Apply a partial update (merge patch) to an existing persona's state or facts. Use this to update mutable state fields mid-conversation (e.g. current_mood, active_argument, heat).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Persona slug to edit"},
                    "patch": {"type": "object", "description": "JSON merge patch — keys to update. Nested keys supported (e.g. {\"state\": {\"heat\": \"rising\"}})"}
                },
                "required": ["name", "patch"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "queue_conversation",
            "description": "Schedule a future conversation between two personas. The heartbeat will pick this up when the channel is quiet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_persona": {"type": "string", "description": "Persona slug initiating the conversation"},
                    "to_persona": {"type": "string", "description": "Persona slug being addressed"},
                    "seed_prompt": {"type": "string", "description": "Opening line or topic for the conversation"}
                },
                "required": ["from_persona", "to_persona", "seed_prompt"]
            }
        }
    }
]

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
    sim_city: bool = False,
    tool_handler=None,
) -> AsyncGenerator[Any, None]:
    from search import web_search as do_web_search
    client = get_client(provider, cfg)
    resp_cfg = cfg.get("response", {})
    temp = temperature if temperature is not None else resp_cfg.get("temperature", 0.7)
    max_tok = max_tokens if max_tokens is not None else resp_cfg.get("max_tokens", 8192)
    start_time = time.time()
    tools_available = use_tools
    tools_list = [WEB_SEARCH_TOOL] + (SIM_CITY_TOOLS if sim_city else [])

    if tools_available:
        try:
            response = await client.chat.completions.create(
                model=model, messages=messages, max_tokens=max_tok, temperature=temp,
                tools=tools_list, tool_choice="auto",
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
        reasoning = getattr(msg_obj, "reasoning_content", None)
        if reasoning:
            yield ("<think>" + reasoning + "</think>", None)
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
            if tc.function.name == "web_search":
                result = await do_web_search(args.get("query", ""), **web_cfg)
            elif tool_handler:
                result = await tool_handler(tc.function.name, args)
            else:
                result = f"Tool '{tc.function.name}' not available in this context"
        except Exception as e:
            result = f"Tool error: {e}"
        return {"role": "tool", "tool_call_id": tc.id, "content": str(result)}

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
    _reasoning_started = False
    _reasoning_closed = False
    async for chunk in stream:
        if chunk.choices:
            delta = chunk.choices[0].delta
            reasoning_chunk = getattr(delta, "reasoning_content", None)
            content_chunk = delta.content
            if reasoning_chunk:
                if not _reasoning_started:
                    yield ("<think>", None)
                    _reasoning_started = True
                yield (reasoning_chunk, None)
            if content_chunk:
                if _reasoning_started and not _reasoning_closed:
                    yield ("</think>", None)
                    _reasoning_closed = True
                yield (content_chunk, None)
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
