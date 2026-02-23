# Psychograph Discord Bot

A multi-persona Discord bot backed by local LLMs via **LM Studio** or cloud models via **OpenRouter**. Each persona is a distinct character with its own voice, expertise, and worldview. Context follows Discord reply chains — multiple independent conversations can coexist in the same channel.

---

## Quick Start

**1. Install dependencies**
```bash
cd DiscordBot
python -m venv venv
venv/Scripts/activate      # Windows
pip install -r requirements.txt
```

**2. Set your Discord token**

Copy `.env.example` to `.env` and fill in your token:
```
DISCORD_TOKEN=your_token_here
```

**3. Configure in `config.yaml`**

The defaults point to a local LM Studio instance. Change `default_provider`, `default_model`, or `persona` as needed.

**4. Run**
```bash
python bot.py
```

---

## Usage

Mention the bot to start a conversation:
```
@bot what do you make of the Epstein network?
```

Reply to a bot message to continue the same thread. Multiple independent conversations can run in the same channel — context follows reply chains, not channel history.

Attach images to any message and the bot will see them (requires a vision-capable model in LM Studio, e.g. Qwen2.5-VL).

---

## Commands

All commands use `@bot <command>`. Chain multiple commands with `;`:
```
@bot reset; persona ledger; verbosity 3
```

### Personas

| Command | Effect |
|---|---|
| `@bot personas` | List all available personas, mark active |
| `@bot persona <name>` | Switch to a different persona (clears channel history) |
| `@bot prompt` | Show the active persona's full system prompt |

### Models & Providers

| Command | Effect |
|---|---|
| `@bot model` | List available models for the current provider |
| `@bot model <name>` | Switch to a specific model |
| `@bot provider local` | Switch to LM Studio (local) |
| `@bot provider openrouter` | Switch to OpenRouter (cloud) |

### Other

| Command | Effect |
|---|---|
| `@bot verbosity <1-5>` | Set response length (default: 2) |
| `@bot reset` | Clear this channel's conversation history |
| `@bot restart` | Restart the bot process |

### Reaction Shortcuts

React to any bot message:

| Emoji | Action |
|---|---|
| 🔄 | **Regenerate** — re-run the response with higher temperature (more variation) |
| 📌 | **Pin** — save the message as a note, injected into future system prompts for this channel |

---

## Verbosity Levels

Control how much the bot writes with `@bot verbosity N`:

| Level | Behaviour |
|---|---|
| `1` | One sentence, full stop |
| `2` | 1–3 sentences — the default |
| `3` | One short paragraph |
| `4` | Full paragraph, substantive |
| `5` | No limit — full depth and character voice |

---

## Personas

| Persona | Character |
|---|---|
| `mecha-epstein` | Investigative reporter who survived, went underground. Obsessed with the Epstein network. Exhausted by what he knows. |
| `the_real_epstein` | Epstein himself — charming, controlled, always in a meeting, never rattled |
| `ledger` | Forensic accountant, 26 years. Follows money. Not cynical, just interested. |
| `sigint_ghost` | Burned-out SIGINT analyst. 19 years in, 8 years out. Reads everything for pattern and anomaly. |
| `cracker` | Safecracker, 31 years. Mechanism before contact. Patience above all. |
| `philoclanker` | Reclusive philosopher-scholar. Analytic and continental. Cites real thinkers. Archaic register. |
| `the_commentator` | Medieval Islamic rationalist. Aristotelian scholar. Sees 800 years at once. |
| `the_coroner` | Forensic pathologist, 12,000 autopsies. The body doesn't lie. |
| `the_merchant` | Venetian merchant, 1490s. Reads everything through trade, information, and risk. |
| `black_widow` | Trial lawyer. Excellent listener. Cannot turn off the analysis. |
| `cassandra` | Failed revolutionary. Specific and unsentimental about how change works. |
| `vostok` | Soviet cosmonaut. Has seen Earth from orbit. Contemptuous of group psychology. |
| `the_naturalist` | Wildlife documentary narrator. Anthropologises the humans. |
| `the_scaffold` | Cognitive scientist (Dr. Mara Voss). Tests understanding via transfer, teaching, and productive failure. |
| `templexity` | Nick Land apostle. Hyperstition. Cities as time machines. Capital as machinic intelligence. |
| `k_punk` | Mark Fisher mourner. Blames Land for the right-acc pipeline. Holds onto what Fisher was building. |
| `strange_loop` | AI safety researcher. Reflective stability focus. |
| `plateau` | Deleuze postdoc. Precise on concepts. Virtuality, cinema, the actual/virtual distinction. |
| `chess` | Chess opponent (~1900 Elo). Responds only to moves in SAN or UCI notation. |
| `pineapple` | 23 years of pineapple on pizza. Has derived a general philosophy from it. Thoroughly at peace. |
| `normal_dude` | Just a normal person on Discord. Not a bot. Why would you even ask. |
| `other_persona` | Generic fallback |

---

## Provider Setup

### LM Studio (local, default)

1. Open LM Studio → load a model → Local Server tab → Start Server
2. The bot connects to `http://localhost:1234/v1` by default
3. For image support, load a vision model: **Qwen2.5-VL**, **LLaVA-1.6**, or **InternVL2**

Switch models at runtime without restarting:
```
@bot model         ← lists loaded models
@bot model <name>  ← switches
```

### OpenRouter (cloud)

1. Get an API key from [openrouter.ai](https://openrouter.ai)
2. Add to `.env`: `OPENROUTER_API_KEY=your_key`
3. Switch provider: `@bot provider openrouter`
4. Pick a model: `@bot model anthropic/claude-sonnet-4-6`

Available OpenRouter models are listed in `config.yaml` under `providers.openrouter.models`. Add any OpenRouter model ID there.

---

## Configuration (`config.yaml`)

```yaml
default_provider: local        # "local" or "openrouter"
default_model: local-model     # persisted across restarts
persona: mecha-epstein         # persisted across restarts

context:
  max_messages: 40             # how deep to follow reply chains

response:
  max_tokens: 8192
  temperature: 0.7
```

Provider, model, and persona changes made via `@bot` commands are written back to `config.yaml` immediately.

---

## Notes

- Only one instance can run at a time (singleton guard on UDP port 47823)
- Conversation history persists across restarts in `history.db`
- `@bot reset` clears history for the current channel only
- Verbosity resets to level 2 on restart (not persisted)
- Pinned notes (📌) persist per-channel and are injected into every response in that channel
