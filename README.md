# Psychograph

A multi-persona Discord bot. Each channel picks a persona; the bot answers when mentioned, when someone replies to one of its messages or when someone talks to a persona by name, and each reply chain keeps its own memory (on MiMo it also sees the recent channel messages that matter to the reply). Inference runs on a local LM Studio server or an on-demand Modal GPU worker that scales to zero, tuned per model.

It only responds in `#sim-city`, `#little-st-james`, `#shitpost`, `#games`, `#zack’s-padded-room`, `#zack’s-waiting-room` and `#leg-day` (and their threads) — set `allowed_channels` in `config.yaml`.

## Using it

Members know the bot as **Mecha Epstein**. Everything goes to the same bot, in three lanes by what answers it:

| Lane | Answered by | How to reach it |
|---|---|---|
| **Chat** | the model, as the channel's persona | **@mention** or **reply**, call a persona by name (*aura-bot thoughts?*, *mochi, thoughts?*), `/ask persona prompt`, right-click → **Apps → Ask persona** |
| **Tools** | Jev checks first, then the model writes | `@bot judge: …`, `@bot minutes`, `@bot steelman …`, `@bot debate review`; right-click → **Review this debate** / **Judge this** |
| **Quick** | Jev alone: about half a second, no GPU | `/quick decide · odds · tier · rate · tone · vibe · chatter`, `@bot decide: …`, `@bot vibe check`; right-click → **Tone check** |

Plain-language mentions are routed by Jev (see [System 1](#system-1-jev)), so *who won that argument?* reaches the debate review and *what are the odds zack mentions his shin* reaches the odds; starting with a tool's or feature's name always works. Every reply's footer says which lane answered.

| | |
|---|---|
| By name | No @ needed. A **chatter** answers to its name plus `-bot` (*hpcr-bot what*, *aura-bot thoughts?*), since its bare name means the real person; that always works. A **character** answers to its bare name (*mochi would hate this*), and the bot to its own display name, when Jev is at least 80% sure it's spoken to rather than the word used in passing; a character name a member also goes by is skipped. |
| Replies | Follow the reply chain, so separate threads don't bleed into each other, and continue with whichever persona gave the answer (a tool's answer goes back to the channel's persona). |
| `tell @someone a poem` | Answers publicly and pings only that member. |
| x.com links | The bot reads the post (Discord's preview, else the public FxEmbed API) as untrusted quoted context. |
| Repeats | Pasting a chat request the bot answered in the last 30 minutes (word for word or near enough) gets a one-line reply linking to that answer instead of another model call. Short messages, commands, tools and quick answers can repeat freely. |
| React **🔁** / **🗑️** on an answer | Regenerate it (the asker), or delete it and forget it (the asker or anyone with Manage Messages). |
| React **🔮** on a message | Log it as a prediction; settle it later with `/scores predictions`. |
| **/help** | A tour of all this inside Discord. |

While it works, the bot reacts to your message: a random server "thinking" emote (**👀** where there's none), **☁️** waking a cold Modal GPU (expect ~30–60s), **⏳** queued behind another reply, **⚠️** failed.

### Commands

| Command | |
|---|---|
| `/ask persona prompt` | A one-off answer from any persona without changing the channel's. |
| `/quick …` | Instant answers: `decide` between options, the `odds` of a yes/no, a `tier` list, `rate` a take, the `tone` of a message, a `vibe` check of the chat, which `chatter` someone sounds like. |
| `/persona name` | Switch the channel persona. The list opens straight away, grouped as chatters, characters, tools and the server's custom personas, with the current one marked; type a group name (`tools`) to filter. |
| `/persona-manage name` | Pick **＋ New persona** to create one (it's selected for the channel), or a persona to open its panel with **Edit**, **Delete** and **Reset picture**, showing only what you're allowed to do. Add `image:` for a new picture. Custom personas: their creator or Manage Server; built-ins (pictures only): server managers. |
| `/status` | The channel's settings, with controls for persona, reply length, **Reactions** (on by default), **Voice**, **Sounds** and resetting history. |
| `/verbosity concise\|balanced\|detailed` · `/reset` | Reply length; clear this channel's history (after confirmation). |
| `/timeout member 30m` | The bot ignores that member (messages, commands and reactions) for `30m`, `2h`, `1d`… up to 7 days, or `off`. Moderators (Timeout Members or Manage Server). |
| `/duel persona persona topic [rounds]` | Two chat personas argue a topic for 1–3 rounds (default 2), each turn one model call posted as that persona; Jev then judges who argued better and picks the line of the duel. One duel per channel at a time. |
| `/scores debates` · `/scores predictions` · `/scores duels` | The debate leaderboard; open predictions to settle, and everyone's track record; persona duel records. |
| `/bot model` · `/bot stats [period]` · `/bot cost` · `/bot digest` | The backend, model and sampling; reply counts, times, cold starts, tokens and top personas; the Modal workspace bill (Manage Server); a private preview of this week's digest (Manage Server). |
| `/sound [name]` · `/chess …` | The soundboard; chess against Stockfish. |

### On its own

- **Reactions.** Now and then the channel's persona reacts to a message nobody sent the bot with the **server emote** that fits: `:kekw:` on a joke that lands, `:blursed:` on something baffling, `:pepothink:` or `:spinningfish:` on a puzzler, `:WooYeah2x:` on a vulgar roast, `:laoma:` on something zen. One Jev call decides whether it lands (at least 85%, and not serious, sad or private) and which mood fits (funny, thinking, baffled, roast, cope, based, zen, sweet, doom, rage, theory, clanker, delicious), from `emotes.json`, which groups the server's emotes by mood with a line on when each mood fits; the emote is then drawn at random from that mood, so the same moment doesn't always get the same one. Emotes the server lacks are skipped. While the bot works on an answer it shows a random emote from the `status` mood (thinking) instead of 👀, and the status line changes hourly, half the time to a real line from a persona's `says`. A persona can lean toward favourites with `"emotes": [...]` in its file (aura likes `:laoma:`). At most once per channel every `ambient_cooldown_minutes` (default 20), with Jev asked about a channel at most once a minute and never about messages under three words. The first message in a channel that's been quiet for four days gets `:mmm:` (`revival` in `emotes.json`). Personas never react to their own answers. **Reactions** in `/status` turns it off for a channel, `ambient_reactions: false` in `config.yaml` everywhere. No model call.
- **The heartbeat.** A few times a day (`heartbeat_per_day`, default 3) in each of `heartbeat_channels` (`#shitpost`, `#leg-day`), at times spread through a UTC window (`heartbeat_hours`, default `13-1`) and fixed per day. Jev reads the last 25 messages and picks whose voice fits. If the channel is live (a member posted in the last 90 minutes) and there's an opening, that persona chimes in once. If it has gone quiet (the last member message is under 18 hours old) and the chat stopped on a loose end, such as a question or claim nobody answered, that persona picks it back up, linking the message it answers. A channel quiet for longer is left alone: no openers into an empty room. Replying to a drop continues with that persona. Each drop is one model call, so it may wake the GPU (about $0.07 with its idle tail).
- **The Monday digest.** At 09:00 UTC every Monday the minutes persona posts the week to `digest_channel` (`#newsroom`) as one plain message: a short editorial in its voice when a tools model is running (else a dateline), the most-reacted messages linked inline, the most-reacted message from this week last year, and the counts as subtext: who talked most, the busiest channels, what the scoreboards gained and the bot's own replies, all from the allowed channels. `/bot digest` previews it privately.

### Personas

- **Voice** (in `/status`, Manage Messages required) makes the bot speak *as* the persona — its own name and avatar — through a channel webhook. The bot needs **Manage Webhooks**; without it, replies stay as embeds. Replies to persona messages continue the conversation as normal.
- Pictures are cropped to a 256px square, per server, and also show on embeds and `/status`. Because Discord attachment links expire, each picture is kept as the avatar of a small "avatar · *name*" webhook in the channel where it was uploaded — don't delete those (a channel holds at most 15 webhooks).
- Persona names can't match a member of the server, and if a member later takes a persona's name, that persona posts as "*name* (persona)". Discord also tags every persona message as an app.

Built-in personas live in `personas/chatters/`, `personas/characters/` and `personas/tools/`; the folder sets the group. Each is a structured `.json` file or a plain `.md` prompt, which may end with a `## Compact` section used for weaker models. Chatters use the persona format, every field short: `voice` (who they are in the room, 2–3 sentences), `register` (how they write, measured from the logs: length, case, punctuation), `says` (6–8 real lines, verbatim, which carry a voice in fewer tokens than any description), `believes`, `moves` (how they argue and joke, and what wakes them up), `people` (one line per key relationship), `bits` (running jokes), `never` (the 1–3 things that break character) and `emotes` (favourite server emotes). The compact voice is derived from `voice`, `register` and the first four `says`; real lines also turn up now and then as the bot's status. Older files may use `facts` and `state` instead, and tools add `channel_context`, `triggers`, `intent` and `system1`; any file may set `reaction`, `avatar`, `compact` and `about`. Avatars default to a generated image seeded by the persona's name.

### What the model sees

For a chat reply, the model gets the persona, the reply chain (up to 100 messages, as conversation turns) and a short transcript of **the channel messages that matter**, then the message to answer, marked `[Reply to this message]`:

- The channel's last 60 messages (minus the reply chain) are candidates. Jev's triage, the same call that routes the message, scores each for relevance to it: the last 6 are always kept, plus any older message it rates relevant, up to 25. A message that stands alone ("hewwo mochi") gets just the last 3. Without Jev, the last 15.
- In every transcript, members appear by display name and the bot's own messages are bracketed: `[you]` for the persona replying, `[bot as zack]` otherwise. Chatter personas share names with real members, so an unbracketed `zack:` would be indistinguishable from Zack himself.
- Answers by other personas in the reply chain (a tool's, say) are quoted as `[bot as judge, quoted for context]` rather than passed off as the current persona's own words.
- The footer shows how many channel messages it saw (*saw 9 channel msgs*).

The channel is fetched once per request (one Discord page of 100 messages) and shared by triage, tools and chat context. Images aren't seen: an attachment shows as `[attachment: name]`.

### Tools

Tool personas have a job rather than a personality. They only run on models whose `models.json` profile sets `"tools": true` (MiMo); on MechaEpstein they say so instead of derailing on a long transcript.

**Calling one.** Switch a channel to a tool with `/persona`, or call one from any channel:
- **By name:** start a mention with the tool's name or a trigger (`@bot judge: …`, `@bot minutes`, `@bot tldr`, `@bot steelman …`, `@bot review the argument`).
- **By meaning:** otherwise Jev's triage sends a fresh mention to a tool only when it's at least 85% sure, so *who won that argument?* reaches the debate review while *who won the euros?* stays with the channel's persona.
- **Right-click** a message → **Apps → Review this debate** or **Judge this**; the transcript ends at that message, which is the easy way to point at an older argument.
- **/ask** `persona:judge` for a one-off.

A tool does one job: reply to its answer and the channel's persona picks the conversation back up, seeing the tool's answer quoted (up to 1,800 characters, with any Jev notes behind it) rather than as its own words.

**What they read.** `channel_context` is how many recent channel messages a tool reads, as a quoted transcript fetched fresh for each request and never stored, so reply-chain memory doesn't fill up with old transcripts. Long pauses are marked so separate conversations stand apart, and the bot's own answers are credited to the persona that gave them. Tools don't play sounds.

| Tool | Reads | Does |
|---|---|---|
| **debate review** | 60 | Finds the motion, tracks clash, dropped points and concessions, names fouls, and gives a decision with a margin (or *no contest* / *split decision*) and a note per speaker. Rules on the debate, not on who's factually right. |
| **judge** | 25, only if the question is about the chat | Restates the question neutrally, weighs the considerations, gives the best case for the other side, and rules from a fixed verdict list, with a proportionate *Remedy* for petty disputes. |
| **minutes** | 100 | *What did I miss?* Deadpan committee minutes grouped by topic, with actions and unresolved questions. |
| **steelman** | 30, only if asked about the chat | The strongest honest case for a position or each side, its weak point, and the crux. Never picks a winner. |

**The debate leaderboard.** After a review, the members it names in bold who spoke in the transcript are scored from its **Decision** line: a win for the winner, a loss for the rest, a split for everyone on a split decision, nothing for *no contest*. `/scores debates` shows the table. Asking again about the same people in the same channel within six hours replaces the earlier result, and deleting a review (🗑️) removes it.

**Predictions.** React 🔮 on a member's message to log it (the bot adds 📌). `/scores predictions` lists the open ones and everyone's track record; pick one to settle it as right, wrong or void. Anyone but the predictor can settle it (moderators always can).

### System 1: Jev

[Jev](https://docs.typesafe.ai/) is TypeSafe's "System One" model: instead of text it returns typed decisions (yes/no probabilities, a choice among options, a score) in about 0.3–0.5 s, billed at $0.042 per million input tokens, with no GPU of ours involved. The bot uses it for the narrow, fast decisions and keeps the language model (System 2) for reasoning and writing. All the questions and thresholds are in `psychograph/jev.py`.

- **Triage**, one call per mention: a Choice of where it goes (chat first, then the tools' and quick features' `intent` descriptions and questions about the bot) and, in the same call, a relevance score for each candidate channel message (see [What the model sees](#what-the-model-sees)). It routes away from chat only at 85% certainty, so a wrong guess falls back to the persona rather than stealing a chat reply. Questions about the bot itself are answered from code: *what can you do?* (the `/help` card), *which persona is this?* (every persona by group), *what model are you on?* (the `/bot model` summary); *who are you?* to a persona stays chat. Quick features and these answers work on any model; tools only on models that run them.
- **Quick answers** (`psychograph/quick.py`): each feature is a few typed questions and a fixed card filled from the answers, since Jev can't write text. Each answer is kept in the reply chain with what Jev was shown and the probabilities it gave, so replying "why 81%?" reaches the channel's persona with the numbers in front of it (and a note that Jev gives probabilities, not reasons). The debate review's System 1 notes are kept with the review the same way.
- **Judging duels**, **choosing who drops in** on the heartbeat, and **noticing** messages nobody sent the bot: whether one that names a persona is spoken to it, and whether one lands well enough for a reaction (above).
- **Debate screening**: before a review, one call asks whether the transcript holds a real argument, tags each member's message as a foul (ad hominem, strawman, moved goalposts, unsupported claim, whataboutism) or as evidence, and picks the side it thinks argued better. With no argument (under 15%) the review answers **No contest** at once and the GPU is never woken. Otherwise the confident tags go to the model as hints to check, and Jev's own pick is kept out of the prompt so the model reasons independently. The pick appears in the footer (*System 1 leaned Leon (90%)*) as a cross-check.
- **Reading verdicts** for the leaderboard: one Choice over the participants, *split* and *no contest*, read from the review's decision line (without Jev, a simple text match).
- **Picking sounds** (see [Sounds](#sounds)) when the model didn't tag one.

Set `JEV_API_KEY` in `.env` to turn it on. Every call fails soft: with no key, an error or a 4-second timeout, the bot carries on without it (no routing by meaning, quick answers or instant answers; the last 15 channel messages as context; no screening; text-matched verdicts; keyword-matched sounds; no answering to names, ambient reactions or heartbeat drops). Messages sent to Jev are recent channel text, the same text the tools send to the model.

### Sounds

Personas can drop short meme sounds into a conversation as Discord **voice messages** (the waveform bubble). Turn on **Sounds** in `/status` (Manage Messages); anyone can play one by hand with **`/sound name`**, or list them with `/sound`.

- The model is given the list of sounds and picks one by ending its reply with `[sound: vine_boom]`; the tag is removed from the text. Weaker models that narrate instead (`*sad trombone*`) count too. Compact-context models get just the names.
- If the model doesn't pick one, Jev judges the exchange: one yes/no on whether the moment suits a sound at all (a joke, a triumph, a reveal; not a sad, sincere or plain reply), and one choice of which sound. It plays only when the moment scores at least 0.75 and the pick is reasonably clear. Without Jev, a keyword match sometimes picks one instead (about a third of the time). Either way an untagged sound plays at most once per two minutes per channel; tagged sounds have a 15-second per-channel cooldown, and `/sound` an 8-second per-person one.
- In **Voice** mode the clip comes from the persona itself as an audio attachment (webhooks can't send voice messages).
- The bot needs **Attach Files** and **Send Voice Messages**; if voice messages are refused it sends a plain audio file.

The bank is built from `sounds/sources.json` (each clip's source file, keywords, mood and description, plus optional `start`/`seconds` trims):

```powershell
python tools/build_soundbank.py          # converts new or changed clips; --force rebuilds all
```

Clips are trimmed of leading silence, capped at 7 seconds, EQ'd (40 Hz high-pass, 13 kHz low-pass, a gentle 3 kHz dip), compressed, levelled to -24 dB RMS in two passes (so even sub-second blips match), peak-limited to about -4 dBFS and encoded as Ogg Opus, with the duration and waveform written to `sounds/sounds.json`. The audio files are git-ignored because they come from a local sample library; restart the bot after building.

### Chess

`/chess new` starts a game in the channel and switches its persona to `chess`; then mention the bot with moves (`e4`, `Nf3`, `e2e4`) or use `/chess move`. Black is played by local Stockfish — never the language model. `/chess commentary on` adds a one-line note from local LM Studio only. If Stockfish fails, your move isn't saved.

## Model profiles

`models.json` is the single list of models. Each entry drives three things:

- **Dashboard presets** (`dash.mjs` → *Choose model*), including storage estimates.
- **Deploy settings** (`modal_app.py`): server context size and the thinking template. Choosing a preset writes them to `.env`; explicit `.env` values still win.
- **Chat tuning** (the bot): context mode, history depth, prompt budget, output length, temperature, top-p, and whether tool personas may run (`"tools": true`). When the configured model matches a profile, these override `.env`'s generic `LLM_TEMPERATURE` / `*_MAX_OUTPUT_TOKENS`.

| | MiMo V2.6 Distill Qwen 9B | MechaEpstein 8000 |
|---|---|---|
| Context mode | **full** — whole persona with facts, 100-message reply chains plus the last 60 channel messages (`channel_messages`), linked posts as JSON | **compact** — short persona voice, last 6 messages clipped to ~400 chars, plain-language rules |
| Tools | on | off |
| Prompt budget | the server's 128k | ≤3,072 tokens |
| Output / sampling | 2,048 tokens · temp 1.0 · top-p 0.95 | 400 tokens · temp 0.8 · top-p 0.9 |
| Server | 128k context (the model is trained to 262k), Qwen thinking on | 40,960 context (its training length), thinking off |

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

Local LM Studio runs on port 1234 with a 4k context by default (2k/4k in *Config*). Modal runs `llama.cpp` on one L4, scales to zero after five idle minutes (`MODAL_SCALEDOWN_SECONDS`, default 300, because replies often take over a minute) and caches model weights in a Modal Volume; at the listed $0.80/GPU-hour, an L4 costs about $0.013 per minute before CPU and memory, and the cold start and the idle tail (about $0.07 for five minutes) are billed too. `/bot cost` and the dashboard budget are displays against a $30 planning budget, not hard caps.

On Windows, Modal 1.6 prints a harmless deprecation warning under Python 3.14; the documented Python 3.12 avoids it.

## Code map

```
psychograph/
  settings.py       one Settings object: .env > config.yaml > defaults
  profiles.py       per-model tuning from models.json
  backends.py       LocalBackend (LM Studio) and ModalBackend → Completion; cold-start and queue tracking
  personas.py       Persona + PersonaRegistry (grouped built-in files, chess, custom), compact prompts, avatars
  store.py          SQLite: reply chains, channel settings, custom personas, chess games, debates, predictions, generation log
  conversation.py   system prompts (full/compact), context fitting, linked posts, channel transcripts, reply cleanup
  responder.py      one request end to end: status reactions → context → model → clean → deliver → record
  webhooks.py       persona voices through a channel webhook, avatar hosting, impersonation guard
  sounds.py         the soundbank: tags, keyword picks, cooldowns, sending voice messages
  render.py         embeds, message splitting, generation stats line
  chess_game.py     ChessService (one load/save per turn, per-channel lock) and a persistent UCI Stockfish
  jev.py            System 1: Jev client, every question and threshold (triage, debate screening, verdicts, sounds)
  quick.py          /quick features: Jev-only answers rendered as cards
  repeats.py        spotting a chat request answered in the last 30 minutes
  duel.py           persona duels: turn prompts and Jev's verdict
  heartbeat.py      unprompted drops: daily slots, drop-ins on live chat, loose ends picked up once quiet
  ambient.py        messages nobody sent the bot: answering to a persona's name, server-emote reactions (emotes.json)
  digest.py         the Monday digest
  speak.py          one in-character message, posted as the persona
  stats.py          /bot stats and `python -m psychograph stats`
  bot.py            the bot, channel allowlist, presence
  cogs/             chat (triage and dispatch, /ask, message commands, 🔁/🗑️), quick (/quick, Tone check),
                    personas (/persona, /persona-manage), settings (/status /verbosity /reset /timeout),
                    tools (/scores, 🔮), ops (/bot model|stats|cost|digest), fun (/duel, heartbeat, digest), soundboard, chess, help
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
