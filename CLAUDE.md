# DiscordBot — Developer Reference

A multi-persona Discord bot backed by local LLMs (LM Studio) or cloud models (OpenRouter). Responds when mentioned. Persists conversation context as a reply-chain graph in SQLite.

## Running

```bash
python bot.py
```

or for the dashboard (experimental):

```bash
node dash.mjs
```

Stop the bot before running again — the singleton guard (`127.0.0.1:47823` UDP) will exit any second instance immediately.

`.env` requires `DISCORD_TOKEN`. Set `OPENROUTER_API_KEY` to use the OpenRouter provider. All other config lives in `config.yaml`.

## Performance & Caching

The bot employs several layers of in-memory caching and concurrency optimizations:

- **LLM Parallelism**: Local models (LM Studio) are serialized via a provider-specific lock to protect VRAM. **Cloud providers (OpenRouter) run in parallel**, allowing multiple simultaneous conversations without blocking.
- **Database CTEs**: Conversation history is retrieved using a **Recursive Common Table Expression (CTE)** in a single SQLite query (`db.get_message_chain`), replacing iterative parent-pointer lookups.
- **Settings Cache**: Channel persona, verbosity, and temperature are cached in memory (invalidated on write) to avoid SQLite I/O on every message.
- **Search Cache**: Web search results are cached for 1 hour by query string in `search.py`.
- **Chess Cache**: Stockfish moves are cached by (FEN, Depth) in `chess_api.py` to provide instant AI turns for known positions.
- **Model Cache**: OpenRouter model lists are cached for 1 hour in `llm.py` to ensure fast slash-command autocomplete.
- **Lazy Assets**: Avatar generation runs in a background thread on startup to prevent blocking the Discord client.

## File Structure

```
DiscordBot/
├── bot.py          # Discord client, commands, reply-chain context, streaming, views
├── llm.py          # Provider routing, async generator, tool calling, image handling, model caching
├── db.py           # SQLite CRUD — messages (reply-chain) + pins + settings caching
├── styles.py       # Per-persona embed styles (color, footer), get_style(), make_embed()
├── board.py        # FEN → chess board rendering (PNG image + ASCII fallback)
├── chess_engine.py # Move validation, board state, game lifecycle (python-chess)
├── chess_api.py    # Stockfish move lookup via chess-api.com (cached)
├── personas.py     # load_persona(), render_persona(), load_persona_style(), list_personas()
├── search.py       # DuckDuckGo web search (async wrapper + result caching)
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
Entry point. All Discord events, commands, and UI components live here.

- `get_llm_lock(provider)` — returns a `local` lock or `None` (parallel) for cloud providers.
- `get_system_prompt(persona, channel_id)` — persona text + pinned notes + meta suffix.
- `ch_persona(channel_id)` — returns active persona; uses `db.get_channel_persona` (cached).
- `ch_verbosity(channel_id)` — returns active verbosity (cached).
- `_db_chain(parent_id)` — fetches conversation chain via `db.get_message_chain` (Recursive CTE).
- `stream_to_discord(gen, reply_target)` — consumes `llm.complete()` generator. **Suppresses `<think>` content during live streaming**.
- `extract_thinking(text)` — splits raw output into `(thinking_text, rest)`.
- `format_thinking_spoiler(thinking, limit=1200)` — wraps thinking in Discord spoiler tags.
- `handle_summarize(channel_id)` — summarizes last 20 messages, respects provider lock.
- `_options_embed(channel_id)` — builds a settings `discord.Embed` for the channel.

**UI Constants:**
- `_THINKING_LINES` — poetic placeholder strings for reasoning models.
- `_THINKING_SPOILER_LIMIT = 1200` — reasoning spoiler character cap.

**Persistent Views:**
- `ResponseView` — registers in `on_ready`. `regen` button re-calls LLM; `pin` button saves notes.
- `OptionsView(channel_id)` — persona dropdown, verbosity buttons, and reset button.

### `llm.py`
All LLM calls go through here. Both providers use `openai.AsyncOpenAI`.

- `get_client(provider, cfg)` — returns a cached `AsyncOpenAI` client.
- `get_openrouter_models(cfg, ...)` — returns cached list of models (1-hour TTL).
- `complete(messages, provider, model, cfg, ...)` — **async generator** yielding text chunks. Executes `web_search` tool if requested.

### `db.py`
SQLite persistence with in-memory caching.

- **`messages`** — reply-chain graph keyed by Discord message ID.
- **`channel_settings`** — persists persona, verbosity (1-5), temperature, and `reset_ts` per channel.
- **`get_message_chain(start_msg_id, limit)`** — **Recursive CTE** that fetches hierarchy in one query.
- **`usage_logs`** — tracks model, provider, tokens, and response time.
- **`chess_games`** — persists FEN and move stack for active games.
- **`_CHANNEL_CACHE`** — in-memory cache for channel settings to avoid redundant I/O.
- **`init_db()`** — handles automatic schema migrations.

### `ui.py`
Discord UI components and interactive views.

- `ResponseView` — persistent view for bot responses (Regenerate, Pin).
- `OptionsView(channel_id)` — dropdown for persona switching and buttons for verbosity/reset.
- `_options_embed(channel_id)` — generates the status embed for the options menu.

### `personas.py`
- `get_persona_metadata(name)` — returns display name and avatar path.
- `render_persona(data)` — flattens JSON persona data into a system prompt.
- `list_personas()` — scans `personas/` directory.

### `styles.py`
- `PERSONA_STYLES` — mapping of persona name → `{"color": int, "footer": str}`.
- `get_style(persona_name, persona_style=None)` — returns style dict (JSON persona file overrides `styles.py`).

### `board.py`
- `fen_to_image(fen)` — PNG rendering using `seguisym.ttf` → `seguiemj.ttf` → `arialbd.ttf`.
- `fen_to_board(fen)` — ASCII fallback.

### `chess_api.py`
- `get_stockfish_move(fen, depth=12)` — fetches move. **Cached by (FEN, depth)** in memory.

### `chess_engine.py`
- `apply_user_move(channel_id, move_text)` — validates human move.
- `apply_bot_move(channel_id, move_text)` — validates LLM move.
- `extract_bot_move(text)` — pulls first SAN/UCI-shaped token.

## Config Reference (`config.yaml`)

```yaml
providers:
  local:
    base_url: http://localhost:1234/v1
    api_key: lm-studio
  openrouter:
    base_url: https://openrouter.ai/api/v1
    models: [...]

default_provider: local
default_model: local-model
persona: mecha-epstein

context:
  max_messages: 40

web_search:
  max_results: 3
  snippet_chars: 300

response:
  max_tokens: 8192
  temperature: 0.7
```

## Commands

Both **Slash Commands** (`/`) and **Prefix Commands** (`!`) are supported. Prefix commands use `bot.process_commands(message)`.

| Command | Effect |
|---|---|
| `reset` | Clears messages for this channel + records `reset_ts` |
| `persona <name>` | Switch persona for this channel + clear history |
| `personas` | List all personas, mark active |
| `prompt` | Print active persona name + system prompt |
| `verbosity <1-5>` | Set verbosity (persisted per-channel) |
| `model <name>` | Switch model (supports dynamic autocomplete) |
| `provider <name>` | Switch provider (`local` or `openrouter`). Also available as `!provider`. |
| `options` | Open interactive settings panel |
| `restart` | Spawn fresh process, close current |

## Context Model

Context follows **Discord reply chains**. `build_context()` uses the `get_message_chain` CTE. A **Reset guard** uses `reset_ts` to prevent "ghost context" from pre-reset threads when using Discord API fallback.

## Persona File Format

Files are stored in `personas/*.md` but use **JSON format**:

```json
{
  "name": "Display Name",
  "voice": "Character prose.",
  "facts": { "key": "knowledge" },
  "style": { "color": "0x4A4A4A", "footer": " tag " }
}
```

## Verbosity Levels

| Level | Instruction | Label |
|---|---|---|
| 1 | ONE sentence. | *whisper mode* |
| 2 | 1-3 sentences. | *concise* |
| 3 | One short paragraph. | *balanced* |
| 4 | A full paragraph. | *expansive* |
| 5 | No limit. Full depth. | *unbound* |

## Thinking / Reasoning Window

1. **During streaming**: reasoning hidden from live edits.
2. **Post-process**: `extract_thinking` splits output.
3. **Display**: reasoning moved to spoiler tag (content field for embeds, prepended for plain text).
