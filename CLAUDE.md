# DiscordBot — Developer Reference

A multi-persona Discord bot backed by local LLMs (LM Studio) or cloud models (OpenRouter). Responds when mentioned. Persists conversation context as a reply-chain graph in SQLite.

## Running

```bash
python bot.py
```

Stop the bot before running again — the singleton guard (`127.0.0.1:47823` UDP) will exit any second instance immediately.

`.env` requires `DISCORD_TOKEN`. Set `OPENROUTER_API_KEY` to use the OpenRouter provider. All other config lives in `config.yaml`.

## File Structure

```
DiscordBot/
├── bot.py          # Discord client, commands, reply-chain context, streaming
├── llm.py          # Provider routing, async generator, tool calling, image handling
├── db.py           # SQLite CRUD — messages (reply-chain) + pins
├── board.py        # FEN → chess board rendering (PNG image + ASCII fallback)
├── chess_engine.py # Move validation, board state, game lifecycle (python-chess)
├── personas.py     # load_persona(), render_persona(), list_personas()
├── search.py       # DuckDuckGo web search (async wrapper)
├── config.yaml     # All non-secret config
├── .env            # DISCORD_TOKEN, OPENROUTER_API_KEY
├── requirements.txt
├── history.db      # SQLite DB (auto-created, gitignored)
└── personas/       # 22 .md persona files
```

## Stack

| Package | Role |
|---|---|
| `discord.py` | Bot framework |
| `openai` | AsyncOpenAI client — used for both LM Studio and OpenRouter |
| `Pillow` | Chess board image rendering |
| `chess` | python-chess — move validation, board state, FEN |
| `ddgs` | DuckDuckGo search |
| `pyyaml` | Config loading |
| `python-dotenv` | Secret injection |

## Module Reference

### `bot.py`
Entry point. All Discord events and commands live here.

- `get_system_prompt(persona, channel_id)` — persona text + pinned notes + meta suffix
- `_build_meta_suffix()` — injects date/time, tool description, verbosity rule
- `build_context(message, bot_id)` — walks Discord reply chain upward, returns `[{role, content}]` ordered oldest-first. Checks DB first (fast path), falls back to Discord API fetch for messages not in DB.
- `stream_to_discord(gen, reply_target)` — consumes the `llm.complete()` async generator, edits a Discord placeholder message every `STREAM_EDIT_INTERVAL = 1.2s`. Returns `(full_raw_text, discord_message)`.
- `clean_response(text)` — strips `<think>...</think>` blocks (including unclosed) and model-specific tool-call syntax leakage (Qwen, Mistral, Llama, Gemma-3).
- `chunk_text(text)` — splits at word/newline boundaries for Discord's 1990-char limit.
- `handle_command(message, cmd)` — dispatches all `@bot` commands, returns `True` if consumed.

### `llm.py`
All LLM calls go through here. Both providers use `openai.AsyncOpenAI` — the only difference is base URL and auth header.

- `get_client(provider, cfg)` — returns a cached `AsyncOpenAI` client for `"local"` or `"openrouter"`.
- `format_image_blocks(attachments)` — async; returns OpenAI `image_url` content blocks. Works identically for both providers. Images not persisted to DB.
- `get_local_models(cfg)` — calls `GET /v1/models` on LM Studio, returns list of loaded model IDs.
- `get_openrouter_models(cfg, paid_only=False)` — fetches model list from OpenRouter API. `paid_only=True` filters to models where `pricing.prompt != "0"`.
- `complete(messages, provider, model, cfg, ...)` — **async generator** yielding text chunks.

**`complete()` flow:**
1. Pass 1 (non-streaming): call with `tool_choice="auto"` to detect tool calls.
2. If `BadRequestError`: model doesn't support tools → fall through to plain streaming.
3. If no tool calls: yield `msg_obj.content` directly (no second pass needed).
4. If tool calls: execute `web_search` in parallel → build updated messages → Pass 2 streaming with `tool_choice="none"`.

### `db.py`
SQLite persistence. Three tables:

**`messages`** — reply-chain graph. Keyed by Discord message ID.
```sql
discord_msg_id INTEGER PRIMARY KEY
parent_msg_id  INTEGER              -- NULL if conversation start
channel_id     INTEGER
role           TEXT                 -- "user" or "assistant"
content        TEXT
```

**`pins`** — per-channel pinned notes, injected into system prompt.
```sql
channel_id INTEGER
content    TEXT                     -- capped at 200 chars, last 5 shown
```

**`chess_games`** — one row per active chess game, keyed by channel.
```sql
channel_id  INTEGER PRIMARY KEY
fen         TEXT                    -- current position FEN (informational)
move_stack  TEXT                    -- space-separated UCI move history (source of truth)
```
`move_stack` is the authoritative record; the board is always rebuilt by replaying moves from the start. `save_chess_game()`, `get_chess_game()`, `delete_chess_game()` are the CRUD functions.

`init_db()` auto-migrates the old per-channel rolling schema (drops the old `messages` table if `discord_msg_id` column is missing).

### `personas.py`
Read-only. No self-modification.

- `load_persona(name)` — reads `personas/{name}.md`, tries JSON parse → `render_persona()`, falls back to raw text if plain `.md`.
- `render_persona(data)` — flattens `voice` + `facts` dict into a readable system prompt string. `state` fields are ignored.
- `list_personas()` — sorted list of `.md` stems in `personas/`.

### `search.py`
- `web_search(query, max_results, snippet_chars)` — async wrapper over a blocking DuckDuckGo call via `run_in_executor`. Returns a formatted markdown block.

### `board.py`
Chess board rendering from FEN strings. Two output modes:

- `fen_to_image(fen)` — renders a PNG image (returns `bytes | None`). Uses Unicode chess piece symbols (♔♕♖♗♘♙♚♛♜♝♞♟) drawn with Segoe UI Symbol font. White pieces rendered as white glyphs with dark outline, black pieces as dark glyphs with light outline. Board colours match chess.com. Returns `None` if Pillow is missing or FEN is invalid.
- `fen_to_board(fen)` — ASCII fallback in a Discord code block using the same Unicode piece symbols. Used when Pillow is unavailable.

Font priority for pieces: `seguisym.ttf` → `seguiemj.ttf` → `arialbd.ttf` → Pillow default. Labels use Arial.

Called from `bot.py` via `extract_board()` which parses `[board: FEN]` tags from LLM output.

### `chess_engine.py`
Move validation and game state management using `python-chess`. One game per channel, persisted to SQLite.

- `is_chess_persona(name)` — returns `True` if the active persona is `chess`.
- `get_board(channel_id)` — loads the `chess.Board` from DB (or fresh starting position).
- `apply_user_move(channel_id, move_text)` — validates + applies the human's move. Returns `(ok, san_or_error, fen)`.
- `apply_bot_move(channel_id, move_text)` — validates + applies the LLM's move. Same return signature.
- `extract_bot_move(text)` — pulls the first SAN/UCI-shaped token from LLM response text.
- `legal_moves_str(channel_id)` — comma-separated SAN list of legal moves.
- `game_status(channel_id)` — human-readable game-over string, or `None` if ongoing.
- `current_fen(channel_id)` — authoritative FEN for the current position.
- `reset_game(channel_id)` — deletes the game from DB.
- `move_number(channel_id)`, `side_to_move(channel_id)` — convenience accessors.

**Integration with `bot.py`:**
1. User's move is validated *before* calling the LLM — illegal moves are rejected immediately.
2. The authoritative FEN, move number, side to move, and full legal-moves list are injected into the system prompt.
3. After the LLM responds, its move is extracted and validated. If illegal, the LLM is re-prompted with the legal moves list (up to `MAX_CHESS_RETRIES = 3` attempts).
4. The `[board: FEN]` tag in the response is overwritten with the engine's authoritative FEN.

## Config Reference (`config.yaml`)

```yaml
providers:
  local:
    base_url: http://localhost:1234/v1
    api_key: lm-studio
  openrouter:
    base_url: https://openrouter.ai/api/v1
    models: [...]          # listed by @bot model when provider=openrouter

default_provider: local    # changed by @bot provider <name>
default_model: local-model # changed by @bot model <name>
persona: mecha-epstein     # changed by @bot persona <name>

context:
  max_messages: 40         # max reply-chain depth sent to LLM

web_search:
  max_results: 3
  snippet_chars: 300

response:
  max_tokens: 8192
  temperature: 0.7
```

Provider, model, and persona changes are written back to `config.yaml` immediately so they survive restarts.

## Commands

`@bot <command>` — command chaining with `;` supported if first segment is a valid command.

| Command | Effect |
|---|---|
| `reset` | Clears all messages for this channel from DB |
| `persona <name>` | Switch persona + clear channel history + write to config |
| `personas` | List all personas, mark active |
| `prompt` | Print active persona name + full rendered system prompt |
| `verbosity <1-5>` | Set response length (not persisted, resets to 2 on restart) |
| `model` | List models for current provider |
| `model <name>` | Switch model (written to config) |
| `model random` | Pick a random paid model (non-zero cost) from OpenRouter |
| `model free random` | Pick a random free model from OpenRouter |
| `provider local` | Switch to LM Studio |
| `provider openrouter` | Switch to OpenRouter |
| `restart` | Spawn fresh process, close current |

## Reaction Commands

React to any bot message:

| Emoji | Action |
|---|---|
| 🔄 | Regenerate — delete old response from DB, rebuild context, re-call LLM at temperature 0.85 |
| 📌 | Pin — save up to 200 chars to `pins` table; injected into system prompt for this channel |

## Persona File Format

```json
{
  "voice": "Character prose — the system prompt text (second person).",
  "facts": { "key": "stable knowledge" },
  "state": { "key": null }
}
```

Only `voice` and `facts` are rendered. `state` is present in files for legacy compatibility but ignored at runtime (self-modification was removed). Plain-text `.md` files work as fallback (the whole file becomes the system prompt).

## Context Model

Context follows **Discord reply chains**, not channel history. A user builds a thread by replying to bot messages. Multiple independent conversations can coexist in the same channel.

`build_context()` traverses the chain by following `parent_msg_id` links upward in DB, up to `max_messages` deep. For messages not in DB (pre-rebuild history, or after `@bot reset`), it falls back to Discord's `channel.fetch_message()`.

## Image Handling

Both providers use the same OpenAI `image_url` format:
```json
{"type": "image_url", "image_url": {"url": "data:image/webp;base64,..."}}
```
Use a proper vision model in LM Studio — `Qwen2.5-VL`, `LLaVA-1.6`, or `InternVL2`. Gemma-3 does not support this format in LM Studio.

## Verbosity Levels

Injected as a named rule at the end of every system prompt (`## Response length — verbosity N/5`).

| Level | Instruction |
|---|---|
| 1 | ONE sentence. |
| 2 | 1-3 sentences. *(default)* |
| 3 | One short paragraph. |
| 4 | A full paragraph. |
| 5 | No limit. Full depth. |

## Logging

`logging.getLogger("bot")` — format `HH:MM:SS LEVEL [bot] message`.

- `INFO` — startup, provider/model/persona changes
- `ERROR` — LLM failures, reaction handler errors

