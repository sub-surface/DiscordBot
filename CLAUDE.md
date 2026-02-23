# DiscordBot — Developer Onboarding

A Discord bot powered by a local LLM via LM Studio. Responds only when mentioned. Runs manually from the terminal — no daemon, no hosting.

## Stack

- `discord.py` — bot framework
- `openai` (AsyncOpenAI) — talks to LM Studio's OpenAI-compatible endpoint at `http://localhost:1234/v1`
- `ddgs` — DuckDuckGo web search
- `python-dotenv` — env config

## Running

```bash
python main.py
```

Requires `.env` with `DISCORD_TOKEN`. Optional: `PERSONA`, `MAX_TOKENS`  (verbosity is runtime-only, not persisted to `.env`).

## File Structure

```
DiscordBot/
├── main.py            # All bot logic
├── .env               # DISCORD_TOKEN, PERSONA, MAX_TOKENS (gitignored)
├── .env.example       # Template
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

`load_persona(name)` reads the file, parses JSON, and renders it into a flat system prompt via `render_persona()`. Plain-text `.md` files are also supported (backward compat fallback).

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
| `vostok` | Soviet cosmonaut, 1963. Never fully came back down. |
| `the_naturalist` | Field researcher studying Homo sapiens on Discord |
| `the_commentator` | Sports/culture commentator voice |
| `black_widow` | Former intelligence asset, now freelance |
| `the_merchant` | Old-world trader, reads people like markets |
| `philoclanker` | Philosopher of mechanism and determinism |
| `templexity` | Nick Land apostle. Cities as time machines. |
| `k_punk` | Mark Fisher mourner. Blames Land for right-acc. |
| `pineapple` | Obsessed with pineapple on pizza. Has derived a working philosophy from it. |
| `normal_dude` | Convincingly human Discord user. Denies being a bot. |
| `strange_loop` | AI safety researcher obsessed with self-modification and reflective stability |
| `plateau` | Deleuze postdoc. Precise about what the concepts actually mean. |

## Self-Modification

Models can update their own (or another persona's) persistent state by emitting special tags in their response. Tags are stripped before the message is displayed.

### Surgical field update (no restart)
```
[FIELD_UPDATE path="state.current_thread"]value[/FIELD_UPDATE]
[FIELD_UPDATE path="facts.known_manufacturers" target=cracker]Sargent & Greenleaf[/FIELD_UPDATE]
```
Uses dot-notation. Writes to disk immediately. Reloads `SYSTEM_PROMPT` in memory if the active persona was updated.

### Full persona rewrite (triggers restart)
```
[PERSONA_UPDATE]{"name":...,"voice":...,"facts":...,"state":...}[/PERSONA_UPDATE]
[PERSONA_UPDATE target=other_persona]{...}[/PERSONA_UPDATE]
```

## Tools (Tag-Based Search)

Rather than injecting search results automatically or using OpenAI function calling (unreliable on small models), the model emits plain-text tags in its first response. These are parsed, executed, and results are injected before a follow-up call produces the final answer.

```
[WEB_SEARCH]query[/WEB_SEARCH]     — DuckDuckGo. For current events, uncertain facts, time-sensitive queries.
[VAULT_SEARCH]query[/VAULT_SEARCH] — Obsidian notes keyword search. Top 3 excerpts returned.
```

**Flow:**
1. Model responds — may include search tags instead of an answer
2. All searches run in parallel (`asyncio.gather`)
3. Results injected as a `user` message: `"Search results:\n\n{results}\n\nNow answer the original question."`
4. Second API call produces the final reply

Multiple tags of either type are supported in one response. Search rounds are not stored in per-channel history. The `TOOLS` list (OpenAI function calling format) remains defined in `main.py` for future use with capable models.

**Vault search implementation:** keyword overlap scoring across all `.md` files at `VAULT_PATH`. Skips `.obsidian`, `.trash`, `DiscordBot` dirs. Top 3 matches by keyword overlap, 600 chars each. Index built once at startup.

## Per-Channel History

Conversation history is stored in `channel_history: defaultdict(list)` keyed by Discord channel ID. History is `[{"role": "user"|"assistant", "content": "..."}]`. It accumulates for the session and is cleared on reset or persona switch.

## Bot Commands

All commands are triggered by mentioning the bot: `@bot <command>`

| Command | Effect |
|---|---|
| `reset` | Clears this channel's history + zeros active persona's state fields |
| `reset all` | Clears all channel histories + zeros all persona states |
| `persona <name>` | Switches persona, resets its state, clears all history |
| `personas` | Lists all available personas, marks active one |
| `prompt` | Prints the active persona name + full rendered system prompt |
| `verbosity <1-5>` | Sets response length. Default 2. Injected into every system prompt. |
| `restart` | Spawns a new process and closes the current one |
| `edit [persona_name] <content>` | Overwrites persona file with raw content, triggers restart |

## Verbosity

Global verbosity level (1–5) is stored in the `verbosity` module variable and injected into every system prompt via `build_meta_suffix()`. It is **not** persisted — resets to 2 on restart.

| Level | Instruction |
|---|---|
| 1 | One sentence only. Extremely terse. |
| 2 | 1-3 sentences. Match the length of a typical Discord message. *(default)* |
| 3 | A short paragraph. Some elaboration is fine. |
| 4 | A full paragraph. Be thorough. |
| 5 | No length restriction. Full character voice. |

## Reasoning Model Handling

`<think>...</think>` blocks are stripped from responses before display. Both closed tags and unclosed (truncated) blocks are handled. `MAX_TOKENS` defaults to 8192 to give reasoning models sufficient budget for thinking before responding.

## Output

Responses are capped at 1990 characters (one Discord message). If the model output exceeds this, it is truncated with `...`. The full untruncated reply is stored in history.

When a model successfully updates its own state or rewrites a persona, the message is appended with Discord subtext:
```
-# ↺ state.current_thread | ledger: facts.known_cases
-# ↺ rewrote cassandra
```
The suffix length is accounted for in the 1990-char budget.

## Image Input

If a message has image attachments, they are fetched via `discord.Attachment.read()`, base64-encoded, and passed as `image_url` content blocks in the current turn's API call. History stores only the text (no base64). Works with any vision-capable model loaded in LM Studio (LLaVA, Qwen-VL, etc.); non-vision models silently ignore the image blocks.

## Config Reference

| Variable | Default | Description |
|---|---|---|
| `DISCORD_TOKEN` | required | Bot token from Discord Developer Portal |
| `PERSONA` | `pineapple` | Persona to load on startup |
| `MAX_TOKENS` | `8192` | Max tokens for LLM response (includes reasoning) |
