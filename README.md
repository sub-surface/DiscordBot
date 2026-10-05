# Psychograph

A multi-persona Discord bot. Each channel picks a persona; the bot answers when mentioned or when someone replies to one of its messages, and each reply chain keeps its own memory. Inference runs on a local LM Studio server or an on-demand Modal GPU worker that scales to zero, tuned per model.

It only responds in `#sim-city`, `#little-st-james`, `#shitpost` and `#games` (and their threads) — set `allowed_channels` in `config.yaml`.

## Using it

| | |
|---|---|
| **@mention** or **reply** | Chat. Replies follow the reply chain, so separate threads don't bleed into each other. |
| `tell @someone a poem` | Answers publicly and pings only that member. |
| x.com links | The bot reads the post (Discord's preview, else the public FxEmbed API) as untrusted quoted context. |
| **/ask** `persona` `prompt` | A one-off answer from any persona without changing the channel's. |
| Right-click a message → **Apps → Ask persona** | The channel's persona responds to someone else's message. |
| React **🔁** on an answer | Regenerate it (the person who asked). |
| React **🗑️** on an answer | Delete it, all parts, and forget it (the asker or anyone with Manage Messages). |
| **/help** | A tour of all this inside Discord. |

While it works, the bot reacts to your message: **👀** thinking, **☁️** waking a cold Modal GPU (expect ~30–60s), **⏳** queued behind another reply, **⚠️** failed.

### Personas and channel settings

- `/persona [name]` shows or switches the channel persona. `/persona-create`, `/persona-edit`, `/persona-delete` manage custom per-server personas (creators, or anyone with Manage Server).
- `/status` shows the channel's settings and model with buttons for persona, reply detail, signature reactions, **Voice** and resetting history.
- **Voice** (in `/status`, Manage Messages required) makes the bot speak *as* the persona — its own name and avatar — through a channel webhook. The bot needs **Manage Webhooks**; without it, replies stay as embeds. Replies to persona messages continue the conversation as normal.
- `/persona-avatar name image` gives a persona its own picture (cropped to a 256px square); `reset:True` returns to the generated one. Custom personas: their creator or a server manager; built-ins: server managers. Pictures are per server and also show on embeds and `/status`. Because Discord attachment links expire, each picture is kept as the avatar of a small "avatar · *name*" webhook in the channel where it was uploaded — don't delete those (a channel holds at most 15 webhooks).
- Persona names can't match a member of the server, and if a member later takes a persona's name, that persona posts as "*name* (persona)". Discord also tags every persona message as an app.
- `/verbosity concise|balanced|detailed`, `/reactions on|off` (a signature emoji on each answer), `/reset` (clears this channel's history after confirmation).

Built-in personas live in `personas/`: structured `.json` files (`voice`, `facts`, `state`, optional `reaction`, `avatar` and `compact`) or plain `.md` prompts, which may end with a `## Compact` section used for weaker models. Avatars default to a generated image seeded by the persona's name.

### Sounds

Personas can drop short meme sounds into a conversation as Discord **voice messages** (the waveform bubble). Turn on **Sounds** in `/status` (Manage Messages); anyone can play one by hand with **`/sound name`**, or list them with `/sound`.

- The model is given the list of sounds and picks one by ending its reply with `[sound: vine_boom]`; the tag is removed from the text. Weaker models that narrate instead (`*sad trombone*`) count too. Compact-context models get just the names.
- If the model doesn't pick one, a keyword match on the conversation sometimes does (about a third of the time, at most once per two minutes per channel). Tagged sounds have a 15-second per-channel cooldown; `/sound` has an 8-second per-person one.
- In **Voice** mode the clip comes from the persona itself as an audio attachment (webhooks can't send voice messages).
- The bot needs **Attach Files** and **Send Voice Messages**; if voice messages are refused it sends a plain audio file.

The bank is built from `sounds/sources.json` (each clip's source file, keywords, mood and description, plus optional `start`/`seconds` trims):

```powershell
python tools/build_soundbank.py          # converts new or changed clips; --force rebuilds all
```

Clips are trimmed of leading silence, capped at 7 seconds, loudness-matched and encoded as Ogg Opus, with the duration and waveform written to `sounds/sounds.json`. The audio files are git-ignored because they come from a local sample library; restart the bot after building.

### Chess

`/chess new` starts a game in the channel and switches its persona to `chess`; then mention the bot with moves (`e4`, `Nf3`, `e2e4`) or use `/chess move`. Black is played by local Stockfish — never the language model. `/chess commentary on` adds a one-line note from local LM Studio only. If Stockfish fails, your move isn't saved.

### Model and stats

- `/model` shows the backend, model, its profile and sampling. `/stats [day|week|month|all]` shows reply counts, average and warm reply times, cold starts, tokens, and top personas for the server. `/cost` shows the Modal workspace bill (Manage Server).

## Model profiles

`models.json` is the single list of models. Each entry drives three things:

- **Dashboard presets** (`dash.mjs` → *Choose model*), including storage estimates.
- **Deploy settings** (`modal_app.py`): server context size and the thinking template. Choosing a preset writes them to `.env`; explicit `.env` values still win.
- **Chat tuning** (the bot): context mode, history depth, prompt budget, output length, temperature and top-p. When the configured model matches a profile, these override `.env`'s generic `LLM_TEMPERATURE` / `*_MAX_OUTPUT_TOKENS`.

| | MiMo V2.6 Distill Qwen 9B | MechaEpstein 8000 |
|---|---|---|
| Context mode | **full** — whole persona with facts, 40-message reply chains, linked posts as JSON | **compact** — short persona voice, last 6 messages clipped to ~400 chars, plain-language rules |
| Prompt budget | the server's 64k | ≤3,072 tokens |
| Output / sampling | 2,048 tokens · temp 1.0 · top-p 0.95 | 400 tokens · temp 0.8 · top-p 0.9 |
| Server | 64k context, Qwen thinking on | 40,960 context (its training length), thinking off |

Every reply is also cleaned before posting: reasoning (`<think>…</think>`) and leaked chat-template tokens are removed, as is the model labelling its own name, and anything after it starts writing someone else's lines (`Leon: …`) — common with smaller models. Each user turn is stored as `Name: message`, so personas know who's talking in busy channels.

Add a model by adding an entry with a `match` substring of its repository/file name.

## Running it

1. Python 3.12 and Node.js. Create the environment and install:

   ```powershell
   py -3.12 -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`. Enable the **Message Content Intent** in the Discord Developer Portal. For persona voices, give the bot **Manage Webhooks**; for status reactions, **Add Reactions**.
3. For chess, install Stockfish and set `STOCKFISH_PATH` (`STOCKFISH_MOVE_TIME`, `STOCKFISH_THREADS`, `STOCKFISH_HASH_MB` limit CPU use).
4. Run `node dash.mjs`:
   - **1 Run bot locally** — `python -m psychograph` with the configured backend.
   - **2 Deploy Modal worker** / **3 Stop** / **6 Test Modal model** (one billed completion) / **7 Follow Modal logs**.
   - **4 Choose model** — LM Studio models, or a `models.json` preset for Modal (redeploy after).
   - **5 Check Modal budget**, **8 Config** (backend and context), **9 Bot stats**, **t Run tests**.

Local LM Studio runs on port 1234 with a 4k context by default (2k/4k in *Config*). Modal runs `llama.cpp` on one L4, scales to zero after 60 idle seconds and caches model weights in a Modal Volume; at the listed $0.80/GPU-hour, an L4 costs about $0.013 per minute before CPU and memory, and the cold start and 60-second idle tail are billed too. `/cost` and the dashboard budget are displays against a $30 planning budget, not hard caps.

On Windows, Modal 1.6 prints a harmless deprecation warning under Python 3.14; the documented Python 3.12 avoids it.

## Code map

```
psychograph/
  settings.py       one Settings object: .env > config.yaml > defaults
  profiles.py       per-model tuning from models.json
  backends.py       LocalBackend (LM Studio) and ModalBackend → Completion; cold-start and queue tracking
  personas.py       Persona + PersonaRegistry (built-in files, chess, custom), compact prompts, avatars
  store.py          SQLite: reply chains, channel settings, custom personas, chess games, generation log
  conversation.py   system prompts (full/compact), context fitting, linked posts, reply cleanup
  responder.py      one request end to end: status reactions → context → model → clean → deliver → record
  webhooks.py       persona voices through a channel webhook, avatar hosting, impersonation guard
  sounds.py         the soundbank: tags, keyword picks, cooldowns, sending voice messages
  render.py         embeds, message splitting, generation stats line
  chess_game.py     ChessService (one load/save per turn, per-channel lock) and a persistent UCI Stockfish
  stats.py          /stats and `python -m psychograph stats`
  bot.py            the bot, channel allowlist, presence
  cogs/             chat (messages, /ask, Ask persona, 🔁/🗑️), personas, settings (/status), ops (/model /stats /cost), soundboard, chess, help
modal_app.py        the Modal llama.cpp worker
tools/              build_soundbank.py (sounds/sources.json → clips + manifest)
models.json         model presets and profiles
dash.mjs            the terminal dashboard
```

History lives in `history.db` (or `DB_PATH`); the schema upgrades itself in place.

## Tests

```powershell
python -m unittest discover -s tests -t .
```

The Stockfish integration tests run when Stockfish is installed and are skipped otherwise. No test calls LM Studio, Modal or Discord.
