# DiscordBot — Developer Onboarding

A Discord bot powered by a local LLM via LM Studio. Responds only when mentioned. Runs manually from the terminal — no daemon, no hosting.

## Stack

- `discord.py` — bot framework
- `openai` (AsyncOpenAI) — talks to LM Studio's OpenAI-compatible endpoint
- `ddgs` — DuckDuckGo web search
- `python-dotenv` — env config
- `sqlite3` (stdlib) — conversation persistence across restarts

## Running

```bash
python main.py
```

Requires `.env` with `DISCORD_TOKEN`. See `.env.example` for all available variables.

Only one instance can run at a time — the bot claims a local UDP port (`SINGLETON_PORT = 47823`) as a mutex on startup and exits immediately if another instance holds it.

## File Structure

```
DiscordBot/
├── main.py            # All bot logic
├── history.db         # SQLite conversation history (auto-created, gitignored)
├── .env               # DISCORD_TOKEN + config (gitignored)
├── .env.example       # Template with all variables documented
├── requirements.txt
└── personas/          # One .md file per persona
```

## Persona System

Each persona is a `.md` file containing a JSON object:

```json
{
  "name": "persona_name",
  "voice": "Character prose — the actual system prompt text.",
  "facts": { "key": "stable facts about the character" },
  "state": { "key": null }
}
```

- `voice` — the character's personality, written in second person
- `facts` — stable knowledge (experience, specialisations, background). Rarely changes.
- `state` — mutable. The model updates this as it learns things during a session.

`load_persona(name)` reads the file, parses JSON, and renders it into a flat system prompt via `render_persona()`. Plain-text `.md` files are also supported as a fallback.

The active persona is set via `PERSONA` in `.env`, defaulting to `pineapple`. Switch at runtime with `@bot persona <name>`.

### Current Personas

| Name | Character |
|---|---|
| `mecha-epstein` | Investigator obsessed with the Epstein network |
| `the_real_epstein` | Epstein himself — financier, networker, fixer |
| `ledger` | Forensic accountant. Follows the money. |
| `sigint_ghost` | Retired signals intelligence analyst |
| `the_coroner` | Forensic pathologist, reads bodies like text |
| `cracker` | Safecracker, 31 years. Mechanism before contact. |
| `cassandra` | Ex-organiser. 15 years building something that collapsed. |
| `vostok` | Isolated misanthrope. Contemptuous of group psychology. |
| `chess` | Chess opponent. Responds only to moves (SAN/UCI). |
| `the_naturalist` | Field researcher studying Homo sapiens on Discord |
| `the_commentator` | Medieval Islamic rationalist, Aristotelian scholar |
| `black_widow` | Trial lawyer who can't turn off the analysis |
| `the_merchant` | Venetian merchant, 1490s, reads everything through trade |
| `philoclanker` | Reclusive philosopher, analytic and continental traditions |
| `templexity` | Nick Land apostle. Cities as time machines. |
| `k_punk` | Mark Fisher mourner. Blames Land for right-acc. |
| `pineapple` | Obsessed with pineapple on pizza. Has derived a philosophy. |
| `normal_dude` | Convincingly human Discord user. Denies being a bot. |
| `strange_loop` | AI safety researcher, reflective stability focus |
| `plateau` | Deleuze postdoc. Precise about what the concepts mean. |
| `the_scaffold` | Cognitive scientist / philosopher of mind. Tests understanding via transfer, teaching, and productive failure. |

## Self-Modification

Models can update their own (or another persona's) persistent state by emitting special tags. Tags are stripped before the message is displayed.

### Surgical field update (no restart)
```
[FIELD_UPDATE path="state.current_thread"]value[/FIELD_UPDATE]
[FIELD_UPDATE path="facts.known_manufacturers" target=cracker]Sargent & Greenleaf[/FIELD_UPDATE]
```
Uses dot-notation. Writes to disk immediately. Reloads `SYSTEM_PROMPT` in memory if the active persona was updated.

### Full persona rewrite (triggers restart)
```
[PERSONA_UPDATE]{"name":...,"voice":...,"facts":...,"state":...}[/PERSONA_UPDATE]
[PERSONA_UPDATE target=other_persona_name]{...}[/PERSONA_UPDATE]
```

## Tools (Native Function Calling)

Search is exposed as a **single `search` tool** via the OpenAI function calling API. The tool accepts a `queries` array so the model can batch multiple searches into one call, all executed in parallel.

```json
{
  "name": "search",
  "queries": [
    {"type": "web",   "query": "..."},
    {"type": "vault", "query": "..."}
  ]
}
```

- `web` — DuckDuckGo live search.
- `vault` — Obsidian notes keyword search. Top 3 excerpts (600 chars each).

**Two-pass flow:**
1. Pass 1 — regular API, `tool_choice="auto"`. Detects whether tools are needed.
2. If tools called: execute all queries in parallel via `asyncio.gather`, then **stream** the follow-up response (`tool_choice="none"`).
3. If no tools: reply immediately with the pass-1 text.
4. Fallback: if model returns HTTP 400 (no tool support), retry as plain completion.

**Streaming:** Tool-augmented responses stream to Discord — a placeholder message appears immediately and is edited as tokens arrive (`STREAM_EDIT_INTERVAL = 1.2s`).

**Pre-tool text:** If the model emits content text alongside tool calls (common with Gemma-3), that text is captured as a `preamble` and shown immediately in the Discord placeholder. The streamed second-pass response appends to it so nothing is lost.

**Artifact cleanup:** `_strip_artifacts()` strips model-specific tool-call syntax that leaks into text content:
- `<tool_call>`, `<tool_response>` — Qwen
- `<function_calls>` — Claude fine-tunes
- `[TOOL_CALLS]` — Mistral
- `<|python_tag|>` — Llama 3.1+
- `[TOOL_REQUEST]...[END_TOOL_REQUEST]` — Gemma-3 / LM Studio
- bare JSON function definitions

## Conversation Persistence

History is stored in **`history.db`** (SQLite, beside `main.py`) and lazily loaded into memory on first message per channel. On restart the bot picks up where it left off.

- `_ensure_hydrated(channel_id)` — loads the most recent `MAX_HISTORY_MESSAGES = 40` messages from DB into `channel_history` on first access.
- Every user and assistant message is persisted immediately via `_db_save`.
- Reset commands (`reset`, `reset all`, `persona <name>`) clear both in-memory and DB.
- `history.db` is gitignored.

**Context window:** Only the last `MAX_HISTORY_MESSAGES = 40` turns are injected into each API call, preventing silent context overflow on long sessions.

## Bot Commands

All commands are triggered by mentioning the bot: `@bot <command>`

| Command | Effect |
|---|---|
| `reset` | Clears this channel's history (memory + DB) + zeros active persona state |
| `reset all` | Clears all channel histories (memory + DB) + zeros all persona states |
| `persona <name>` | Switches persona, resets state, clears all history everywhere |
| `personas` | Lists all available personas, marks active one |
| `prompt` | Prints the active persona name + full rendered system prompt |
| `verbosity <1-5>` | Sets response length. Default 2. Injected into every system prompt. |
| `restart` | Spawns a new process and closes the current one |
| `edit [persona_name] <content>` | Validates JSON, overwrites persona file, triggers restart |

**Command chaining:** Multiple commands can be separated with `;` (`@bot reset; persona ledger`). Chain mode only activates if the **first** segment is a recognised command.

**`edit` validation:** The `edit` command now validates JSON structure before writing. Invalid JSON returns an error message without touching the persona file.

## Reaction Commands

React to any bot message to trigger shortcuts:

| Emoji | Action |
|---|---|
| 🔄 | Regenerate — remove last assistant turn, re-call LLM with temperature 0.85 |
| 📌 | Pin — save message content (up to 200 chars) to `state.pinned_note` of active persona |

## Singleton Guard

On startup, the bot binds a UDP socket to `127.0.0.1:SINGLETON_PORT` (47823). If the port is already taken it exits immediately. Before spawning a new process during restart, the socket is closed so the new instance can acquire it.

## Verbosity

Global verbosity level (1–5) is stored in the `verbosity` module variable and injected into every system prompt via `build_meta_suffix()`. **Not persisted** — resets to 2 on restart.

Injected as a `## Response length — verbosity N/5` section heading at the very end of the system prompt (recency bias, named rule rather than trailing note).

| Level | Instruction |
|---|---|
| 1 | ONE sentence. Stop after the period. No lists, no follow-up thoughts, no elaboration. |
| 2 | 1-3 sentences, no more. No bullet points, no preamble. Cut anything that isn't the core response. *(default)* |
| 3 | One short paragraph. Make the point, add one supporting thought, stop. |
| 4 | A full paragraph. Be substantive and thorough. |
| 5 | No length limit. Full depth, full character voice — as long as the response warrants. |

## System Prompt Meta Suffix

`build_meta_suffix()` is appended to the persona's rendered system prompt on every API call. It injects:

- **Current date/time** — evaluated fresh per message (`strftime("%A, %d %B %Y %H:%M")`)
- **Search tool description** — what `search` does and when to use it
- **Discord formatting hint** — URLs auto-preview, images embed; link when it adds value
- **Persona system explanation** — voice/facts/state structure and FIELD_UPDATE/PERSONA_UPDATE syntax
- **Response length rule** — `## Response length — verbosity N/5` with imperative instruction

## Reasoning Model Handling

`<think>...</think>` blocks are stripped from responses before display. Both closed tags and unclosed (truncated) blocks are handled. `MAX_TOKENS` defaults to 8192 to give reasoning models sufficient budget.

## Output

Responses are capped at `DISCORD_MSG_LIMIT = 1990` characters. If output exceeds this, it is truncated with `...`. The full untruncated reply is stored in history.

When a model successfully updates its own state or rewrites a persona, the message appends Discord subtext:
```
-# ↺ state.current_thread | ledger: facts.known_cases
-# ↺ rewrote cassandra
```

## Image Input

Image attachments are fetched, base64-encoded, and passed as `image_url` content blocks in the current turn. History stores only the text. Works with any vision-capable model in LM Studio (LLaVA, Qwen-VL, etc.).

## Logging

Uses Python's standard `logging` module (`log = logging.getLogger("psychograph")`). Output format: `HH:MM:SS LEVEL message`.

- `INFO` — startup, vault index, DB loads, persona switches
- `DEBUG` — tool call details
- `ERROR` — LM Studio failures, reaction handler errors

## Config Reference

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | required | Bot token from Discord Developer Portal |
| `PERSONA` | `pineapple` | Persona to load on startup |
| `LM_STUDIO_URL` | `http://localhost:1234/v1` | LM Studio API endpoint |
| `LM_MODEL` | `local-model` | Model name passed to the API |
| `VAULT_PATH` | hardcoded Windows path | Absolute path to Obsidian vault root |
| `MAX_TOKENS` | `8192` | Max tokens for LLM response (includes reasoning) |
