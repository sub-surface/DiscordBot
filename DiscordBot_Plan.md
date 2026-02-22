# Philoclanker — Discord Bot Plan

A Discord bot powered by a local LLM via LM Studio. Responds when mentioned. Maintains per-channel conversation history. Run manually from the terminal.

---

## Architecture

```
[LM Studio — model loaded, local server on :1234]
        ↕ OpenAI-compatible HTTP API
[main.py — discord.py bot]
        ↕ Discord Gateway
[Discord server]
```

**Stack:**
- Python 3.14 + discord.py
- `openai` Python client pointed at LM Studio (`http://localhost:1234/v1`)
- `python-dotenv` for token management
- LM Studio handles model loading, GPU offload, inference

---

## Current State

| Item | Status |
|---|---|
| Git repo initialised | ✅ |
| venv + deps installed | ✅ (`discord.py`, `openai`, `python-dotenv`) |
| `main.py` | ✅ |
| `.env` with Discord token | ✅ |
| `.gitignore` | ✅ |
| Bot online and responding | ✅ |
| `@bot reset` clears channel context | ✅ |
| `<think>` block stripping | ✅ |
| Obsidian vault MCP integration | 🔲 Next |

---

## Character: Philoclanker

Reclusive philosopher-scholar. Genuine expertise across analytic and continental traditions. Strong idiosyncratic positions. Tolerates no shallow takes. Archaic register with moments of bluntness. Engages deeply on: philosophy of mind, logic/maths foundations, political philosophy, metaphysics, ethics, history of ideas.

System prompt lives at `main.py:12` — edit freely.

---

## Running the Bot

```bash
# 1. Open LM Studio → load model → Local Server tab → Start Server
# 2. In terminal:
cd DiscordBot
venv/Scripts/activate
python main.py
```

Stop with `Ctrl+C`. Context resets on restart (in-memory only).

---

## Next: Obsidian Vault Integration

Give the bot read access to an Obsidian vault so it can reference personal philosophy notes during responses.

**Approach:** On each message, search vault markdown files for relevant notes and inject top matches into the system context ahead of the conversation history.

**Steps:**
1. Get vault path from user
2. Add vault search function (keyword/fuzzy match across `.md` files)
3. Inject top-k relevant note excerpts as a context block before history
4. Keep injected context within token budget

---

## Key Parameters (main.py)

| Parameter | Value | Notes |
|---|---|---|
| `LM_STUDIO_URL` | `http://localhost:1234/v1` | Change port if LM Studio uses a different one |
| `max_tokens` | `512` | Increase for longer responses |
| `temperature` | `0.7` | Lower = more focused |
| `CONTEXT_SIZE` | per-channel history list | No hard cap yet — watch token limits with long sessions |
