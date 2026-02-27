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

## File Structure

```
DiscordBot/
├── bot.py          # Discord client, commands, reply-chain context, streaming, views
├── llm.py          # Provider routing, async generator, tool calling, image handling
├── db.py           # SQLite CRUD — messages (reply-chain) + pins
├── styles.py       # Per-persona embed styles (color, footer), get_style(), make_embed()
├── board.py        # FEN → chess board rendering (PNG image + ASCII fallback)
├── chess_engine.py # Move validation, board state, game lifecycle (python-chess)
├── chess_api.py    # Stockfish move lookup via chess-api.com (chess-classic persona)
├── personas.py     # load_persona(), render_persona(), load_persona_style(), list_personas()
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
Entry point. All Discord events, commands, and UI components live here.

- `get_system_prompt(persona, channel_id)` — persona text + pinned notes + meta suffix
- `_build_meta_suffix(verbosity)` — injects date/time, tool description, verbosity rule (takes verbosity int directly)
- `ch_persona(channel_id)` — returns the active persona for a channel: reads `channel_settings` DB, falls back to `config.yaml` default. **All persona lookups go through here — no global state.**
- `ch_verbosity(channel_id)` — returns the active verbosity level for a channel (delegates to `db.get_channel_verbosity`). **Per-channel, persisted in DB.**
- `build_context(message, bot_id)` — walks Discord reply chain upward, returns `[{role, content}]` ordered oldest-first. Checks DB first (fast path), falls back to Discord API fetch for messages not in DB. **Reset guard:** loads `reset_ts` from `channel_settings`; if the Discord API fallback reaches a message predating the last `@bot reset`, it stops the chain there — prevents "ghost context" from old threads.
- `stream_to_discord(gen, reply_target)` — consumes the `llm.complete()` async generator, edits a Discord placeholder message every `STREAM_EDIT_INTERVAL = 1.2s`. Returns `(full_raw_text, discord_message)`. Placeholder chosen randomly from `_THINKING_LINES`. **Suppresses `<think>` content during live streaming** — the placeholder stays as the poetic "thinking…" line while the model reasons; the actual response starts streaming only after `</think>`.
- `extract_thinking(text)` — splits raw LLM output into `(thinking_text, rest)`. Handles both closed `<think>…</think>` and unclosed `<think>…` (model stopped mid-reasoning). Returns `("", text)` if no thinking block.
- `format_thinking_spoiler(thinking, limit=_THINKING_SPOILER_LIMIT)` — wraps thinking text in Discord spoiler tags with a `-# 💭 *reasoning · click to expand*` header. Truncates at `limit` chars (default 1200) with a `-# *(truncated)*` suffix. Returns `None` if thinking is empty.
- `clean_response(text)` — strips model-specific tool-call syntax leakage (Qwen, Mistral, Llama, Gemma-3). **No longer strips `<think>` blocks** — that's now done by `extract_thinking` before `clean_response` is called.
- `chunk_text(text, limit=1990)` — splits at word/newline boundaries. Pass `limit=EMBED_DESC_LIMIT` (4096) for embed responses.
- `handle_command(message, cmd, *, _out: list[str] | None = None)` — dispatches all `@bot` commands, returns `True` if consumed. When `_out` is provided, output is appended to that list instead of sent as a reply (used for command chaining). Persona and verbosity changes write to `channel_settings` DB (per-channel).
- `_options_embed(channel_id)` — builds a settings `discord.Embed` showing the current persona, verbosity, provider, and model **for the given channel**.

**UI Constants:**
- `_THINKING_LINES` — list of 20 poetic placeholder strings, one chosen randomly per response
- `_THINKING_SPOILER_LIMIT = 1200` — max chars shown in the reasoning spoiler before truncation
- `VERBOSITY_LABELS` — dict mapping levels 1–5 to a poetic label shown in verbosity confirmation messages

**Response sending pattern** (all three paths — `on_message`, regen button, reaction):
```python
thinking, raw_rest = extract_thinking(raw)
cleaned = clean_response(raw_rest)
thinking_display = format_thinking_spoiler(thinking)  # None if no thinking
if style:
    # Embed: thinking in message content (above embed), response in embed description
    await reply_msg.edit(content=thinking_display, embed=make_embed(chunks[0], style), view=view)
else:
    # Plain text: thinking truncated at 800 chars, prepended to response
    short_display = format_thinking_spoiler(thinking, limit=800)
    full_content = (short_display + "\n\n" + cleaned) if short_display and cleaned else (...)
    await reply_msg.edit(content=chunks[0], view=view)
```

**Persistent Views:**

`ResponseView` — attached to every LLM response (except chess personas). Registers itself in `on_ready` via `bot.add_view(ResponseView())` so buttons survive restarts.

- `regen` button (`custom_id="psychograph:regen"`) — strips the current view, rebuilds context, re-calls LLM at temperature 0.85, posts a new response with a fresh `ResponseView`.
- `pin` button (`custom_id="psychograph:pin"`) — saves embed description (or message content) as a pin, sends ephemeral confirmation.

`OptionsView(channel_id)` — 120-second-timeout view for the `@bot options` settings panel. Takes `channel_id` so all interactions read/write the correct channel's settings.

- Row 0: `PersonaSelect(channel_id)` dropdown (up to 25 personas, active one pre-selected using `ch_persona(channel_id)`). On select: writes to `db.set_channel_persona`, clears channel history, edits the options embed in-place.
- Row 1: verbosity buttons 1–5. Active level highlighted green (`ButtonStyle.success`) in `__init__` based on `ch_verbosity(channel_id)`. On click: writes to `db.set_channel_verbosity`, edits the options embed in-place.
- Row 2: "↺ reset context" button (`ButtonStyle.danger`). Clears DB for this channel (records `reset_ts`), sends ephemeral confirmation.

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
SQLite persistence. Four tables:

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

**`channel_settings`** — per-channel persistent settings.
```sql
channel_id  INTEGER PRIMARY KEY
persona     TEXT                    -- active persona name (NULL = use config.yaml default)
verbosity   INTEGER DEFAULT 2       -- 1–5 verbosity level, persisted across restarts
reset_ts    REAL                    -- Unix timestamp of last @bot reset (NULL if never)
```
Functions: `get_channel_persona(channel_id)`, `set_channel_persona(channel_id, persona)`, `get_channel_verbosity(channel_id)` (returns 2 if no row), `set_channel_verbosity(channel_id, verbosity)`, `get_channel_reset_ts(channel_id)` (returns `float | None`).

`clear_channel(channel_id)` deletes all messages for the channel **and** records `reset_ts = time.time()` in `channel_settings`. This timestamp is the fence used by `build_context` to prevent ghost context.

`init_db()` auto-migrates old schemas: drops `messages` table if `discord_msg_id` column is missing; adds `reset_ts` column to `channel_settings` if it predates that column.

### `styles.py`
Embed styling for every persona. No side effects — pure data + two helper functions.

- `PERSONA_STYLES` — dict mapping persona name → `{"color": int, "footer": str}`. All 21 personas have entries. Personas not in this dict (and without a `"style"` key in their JSON file) render as plain text.
- `EMBED_DESC_LIMIT = 4096` — embed description character cap (Discord limit).
- `get_style(persona_name, persona_style=None)` — returns style dict from the JSON persona's `"style"` key if present, otherwise falls back to `PERSONA_STYLES`. Returns `None` if no style found (→ plain text).
- `make_embed(text, style)` — builds a `discord.Embed` with `description=text`, `color`, and optional `footer`.

To add or change a persona's style, edit the `PERSONA_STYLES` dict in `styles.py`, or add a `"style"` key to the persona's JSON file (takes precedence).

### `personas.py`
Read-only. No self-modification.

- `load_persona(name)` — reads `personas/{name}.md`, tries JSON parse → `render_persona()`, falls back to raw text if plain `.md`.
- `render_persona(data)` — flattens `voice` + `facts` dict into a readable system prompt string. `state` fields are ignored.
- `load_persona_style(name)` — reads the same file, extracts and returns the `"style"` dict if the file is valid JSON and has one. Returns `None` otherwise (plain-text personas get their style from `PERSONA_STYLES` in `styles.py`).
- `list_personas()` — sorted list of `.md` stems in `personas/`.

### `search.py`
- `web_search(query, max_results, snippet_chars)` — async wrapper over a blocking DuckDuckGo call via `run_in_executor`. Returns a formatted markdown block.

### `board.py`
Chess board rendering from FEN strings. Two output modes:

- `fen_to_image(fen)` — renders a PNG image (returns `bytes | None`). Uses Unicode chess piece symbols (♔♕♖♗♘♙♚♛♜♝♞♟) drawn with Segoe UI Symbol font. White pieces rendered as white glyphs with dark outline, black pieces as dark glyphs with light outline. Board colours match chess.com. Returns `None` if Pillow is missing or FEN is invalid.
- `fen_to_board(fen)` — ASCII fallback in a Discord code block using the same Unicode piece symbols. Used when Pillow is unavailable.

Font priority for pieces: `seguisym.ttf` → `seguiemj.ttf` → `arialbd.ttf` → Pillow default. Labels use Arial.

Called from `bot.py` via `extract_board()` which parses `[board: FEN]` tags from LLM output.

### `chess_api.py`
Single async function for the chess-classic persona.

- `get_stockfish_move(fen, depth=12)` — POSTs to `https://chess-api.com/v1` with `{"fen": fen, "depth": depth}` using aiohttp. Returns the parsed JSON dict on success (caller uses `.move` for UCI and `.san` for SAN), or `None` on any failure. Handles HTTP errors, missing `move` key, and chess-api.com's `type=info` error envelope. 15-second timeout per request.

### `chess_engine.py`
Move validation and game state management using `python-chess`. One game per channel, persisted to SQLite.

- `is_chess_persona(name)` — returns `True` if the active persona is `chess` (LLM-based).
- `is_chess_classic_persona(name)` — returns `True` if the active persona is `chess-classic` (API-based).
- `is_any_chess_persona(name)` — returns `True` for either chess persona; used by reset/switch guards and to suppress `ResponseView` buttons.
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

`@bot <command>` — command chaining with `;` supported. When commands are chained, all output is collected and posted as a **single reply** after all commands complete.

| Command | Effect |
|---|---|
| `reset` | Clears all messages for this channel from DB; records `reset_ts` to fence context |
| `persona <name>` | Switch persona for this channel + clear channel history (writes to `channel_settings` DB) |
| `personas` | List all personas, mark active |
| `prompt` | Print active persona name + full rendered system prompt |
| `verbosity <1-5>` | Set verbosity for this channel (persisted per-channel in `channel_settings` DB) |
| `model` | List models for current provider |
| `model <name>` | Switch model (written to config) |
| `model random` | Pick a random paid model (non-zero cost) from OpenRouter |
| `model free random` | Pick a random free model from OpenRouter |
| `provider local` | Switch to LM Studio |
| `provider openrouter` | Switch to OpenRouter |
| `options` | Open interactive settings panel (persona dropdown, verbosity buttons, reset) |
| `restart` | Spawn fresh process, close current |

## Buttons (on every LLM response)

`ResponseView` is attached to every bot response (not chess). Both buttons are persistent — they survive bot restarts via stable `custom_id` values registered in `on_ready`.

| Button | Action |
|---|---|
| `↺ regenerate` | Re-runs the response with temperature 0.85 |
| `📌 pin` | Saves the message as a persistent note for this channel |

## Reaction Commands

Reactions still work as an alternative to buttons:

| Emoji | Action |
|---|---|
| 🔄 | Regenerate — delete old response from DB, rebuild context, re-call LLM at temperature 0.85 |
| 📌 | Pin — save up to 200 chars to `pins` table; injected into system prompt for this channel |

## Persona File Format

```json
{
  "voice": "Character prose — the system prompt text (second person).",
  "facts": { "key": "stable knowledge" },
  "state": { "key": null },
  "style": {
    "color": "0x4A4A4A",
    "footer": "· in-character footer text ·"
  }
}
```

Only `voice` and `facts` are rendered into the system prompt. `state` is present in files for legacy compatibility but ignored at runtime. `style` is optional — if present, it overrides the entry in `styles.py` for that persona. Plain-text `.md` files work as fallback (the whole file becomes the system prompt); their style comes from `PERSONA_STYLES` in `styles.py`.

## Embed Styling System

Every persona has an entry in `PERSONA_STYLES` in `styles.py` with a `color` (hex int) and `footer` (in-character tagline). When a persona has a style, responses are sent as Discord embeds (`discord.Embed`) instead of plain text. This gives:

- A colored left-sidebar accent
- A persistent footer with the persona's tagline
- A higher single-message character limit (4096 vs 2000)

To add a new persona to the embed system, add an entry to `PERSONA_STYLES`. To override from within the persona file itself, add a `"style"` key to the JSON.

## Context Model

Context follows **Discord reply chains**, not channel history. A user builds a thread by replying to bot messages. Multiple independent conversations can coexist in the same channel.

`build_context()` traverses the chain by following `parent_msg_id` links upward in DB, up to `max_messages` deep. For messages not in DB, it falls back to Discord's `channel.fetch_message()`.

**Reset guard:** `build_context` loads `reset_ts` from `channel_settings` at the start. When the Discord API fallback fetches a message whose `created_at` timestamp predates `reset_ts`, it stops walking the chain. This prevents "ghost context" — old messages re-entering context after `@bot reset` because a user replied to a pre-reset thread.

## Image Handling

Both providers use the same OpenAI `image_url` format:
```json
{"type": "image_url", "image_url": {"url": "data:image/webp;base64,..."}}
```
Use a proper vision model in LM Studio — `Qwen2.5-VL`, `LLaVA-1.6`, or `InternVL2`. Gemma-3 does not support this format in LM Studio.

## Verbosity Levels

Injected as a named rule at the end of every system prompt (`## Response length — verbosity N/5`). **Verbosity is per-channel and persists across restarts** (stored in `channel_settings.verbosity`; defaults to 2 for new channels).

| Level | Instruction | Label shown to user |
|---|---|---|
| 1 | ONE sentence. | *whisper mode · one sentence, then silence* |
| 2 | 1-3 sentences. *(default)* | *concise · a breath, not a speech* |
| 3 | One short paragraph. | *balanced · a thought, fully formed* |
| 4 | A full paragraph. | *expansive · room to stretch out* |
| 5 | No limit. Full depth. | *unbound · full depth, full voice, no ceiling* |

## Thinking / Reasoning Window

Models that use `<think>…</think>` blocks (e.g., DeepSeek-R1, QwQ) get a collapsible reasoning display. The pipeline:

1. **During streaming** — `<think>` content is stripped from live edits. The placeholder "thinking…" line stays visible while the model reasons; the actual response starts streaming after `</think>`.
2. **After streaming** — `extract_thinking(raw)` splits the full output into `(thinking_text, rest)`.
3. **Display** — `format_thinking_spoiler(thinking)` wraps the reasoning in Discord spoiler tags (`||…||`) with a `-# 💭 *reasoning · click to expand*` header above it.
4. **Placement**:
   - For embed personas: thinking goes in the Discord message `content` field (plain text, above the embed), response goes in embed description — separate character budgets.
   - For plain-text personas: thinking (truncated at 800 chars) is prepended to the response content.

## Logging

`logging.getLogger("bot")` — format `HH:MM:SS LEVEL [bot] message`.

- `INFO` — startup, provider/model/persona changes
- `ERROR` — LLM failures, reaction handler errors
