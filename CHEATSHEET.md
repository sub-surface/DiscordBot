# Bot Cheat Sheet

Mention the bot to chat. Use **Slash Commands** (`/`) for all settings.

## ⚙️ Settings & UI
- `/options` — Open the interactive settings panel (Persona, Model, Verbosity, Temperature, Reset, Summarize).
- `/help` — Show this guide.

## 👤 Personas
- `/personas` — List all available personas (active one marked).
- `/persona <name>` — Switch active persona and clear history.
- `/prompt` — Show current persona's full system prompt.

## 🧠 Memory & Context
- `/reset` — Clear this channel's conversation history.
- `/context` — See the current messages being sent to the LLM.
- `/options` → **[📝 summarize]** — Condense history into a concise summary.

## 🤖 Model & Provider
- `/provider <name>` — Switch between `local` (LM Studio) and `openrouter`.
- `/model <name>` — Switch active model (includes autocomplete for local cache).
- `/temperature <0.0-2.0>` — Set model creativity/randomness.

## 💬 Output Control
- `/verbosity <1-5>` — Set response length (1: whisper, 5: unbound).

## ♟️ Chess
- `/resign` — Resign the current game (public).
- `/level <1-8>` — Set Stockfish difficulty level.

## 🛠️ Meta
- `/restart` — Reboot the bot process (Owner only).
- `/sync` — Force-refresh slash commands in the current guild (Owner only).

### **Buttons (on every response)**
- `↺ regenerate` — Re-run the response.
- `📌 pin` — Save the message as a persistent note for this channel.

The bot has **Web Search** — it decides when to use it automatically.
