# Psychograph

A small Discord bot that responds in DMs, when mentioned, or when someone replies to it. Choose a persona per channel. Run inference locally with LM Studio or call an on-demand Modal model worker from the local bot.

When tagged with a public X/Twitter post link, the bot uses Discord's preview text when available or retrieves public post text through the FxEmbed API. Retrieved text is treated as untrusted quote context; private, deleted, or unavailable posts are not inferred from the URL. Model replies are shown in persona-labeled embeds with generation time and throughput. Modal uses llama.cpp's reported eval tokens, eval time, and tokens per second; the local backend uses an approximate token estimate. Requests such as `tell @member a poem` produce a public channel reply and ping only that explicitly addressed member; the bot does not send DMs.

## Commands

- `/persona [name]` shows or selects the channel persona.
- `/persona-create` opens a form to create and select a custom persona for this server; `/persona-edit` and `/persona-delete` manage custom personas you created.
- `/reactions [on|off]` toggles the channel's persona signature reactions (Manage Messages permission required).
- `/reset` clears conversation history for the channel.
- `/verbosity [concise|balanced|detailed]` shows or sets reply detail for the channel.
- `/model` shows the configured inference backend and model target; Modal model changes are made in the dashboard and require redeployment.
- `/cost` shows Modal workspace usage for the current month (Manage Server permission in guilds).
- `/status` shows the channel persona, reply detail, reaction state, backend, model, and context budget. Its controls switch persona, cycle reply detail, toggle reactions (Manage Messages required), or clear history after confirmation.
- `/chess new`, `/chess move`, `/chess board`, and `/chess resign` manage a channel's chess game.

Chess also accepts moves when the channel's persona is `chess` and the bot is mentioned or replied to.
Chess moves are selected by local Stockfish on the bot's CPU; move generation uses neither LM Studio nor Modal. Install Stockfish separately and set `STOCKFISH_PATH` to its executable (or put it on `PATH`). `/chess commentary on` optionally adds a note using local LM Studio only; commentary defaults off and never uses Modal. `/chess commentary off` returns to CPU-only chess. If Stockfish is unavailable, the submitted move is rolled back.
The bot only handles messages and slash commands in `#sim-city`, `#little-st-james`, `#shitpost`, and `#games`. Chess commands and the `chess` persona are available in `#games`.

Custom personas are stored per server in `history.db`, can contain up to 3,500 characters of instructions, and are selectable with `/persona`. Creators can edit or delete their personas; server managers can manage any custom persona. Deleting one returns channels using it to the default persona. Persona reactions are off by default; `/reactions on|off` requires Manage Messages, and the bot needs Add Reactions permission. When enabled, it adds one signature emoji to its reply and uses a small default emoji for custom personas. `/status` shows whether reactions are active in the channel.

## Local

1. Install Python 3.12 and Node.js.
2. Create and activate an environment, then install dependencies:

   ```powershell
   py -3.12 -m venv venv
   .\venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and set `DISCORD_TOKEN`. The default local model name matches the Mernik GGUF shown in LM Studio; `node dash.mjs` can list loaded models and save another choice.
4. Install the Stockfish engine for your platform and set `STOCKFISH_PATH` in `.env` to its executable. `STOCKFISH_MOVE_TIME`, `STOCKFISH_THREADS`, and `STOCKFISH_HASH_MB` control CPU use.
5. For chat and optional chess commentary, load the model in LM Studio and start its OpenAI-compatible server on port `1234`.
6. Run `node dash.mjs` and choose **Run locally**.

Context follows the replied-to message chain rather than accumulating a channel-wide transcript, and walks back at most 40 stored messages. When needed, the bot drops oldest complete user turns (including multi-message answers) and preserves the latest request; it reserves the configured output allowance and warns when it trims or approaches the limit. The token estimate is a UTF-8 byte heuristic, not the model's tokenizer, so the effective budget is intentionally approximate. `/reset` clears conversation rows for the current channel or thread only; persona and verbosity settings remain. Other channels and threads keep independent history. Records persist in `history.db` until that channel is reset.

Local prompts are capped at 4k tokens by default, leaving output headroom. Configure 2k/4k from the dashboard. Enable the Message Content Intent for the bot in the Discord Developer Portal.

On Windows, Modal 1.6.0 emits a non-fatal deprecation warning under Python 3.14 for its event-loop compatibility policy. At a convenient maintenance point, recreate the virtual environment with the documented Python 3.12, reinstall requirements, and run the tests; do not patch files in `site-packages`.

## Modal

Modal inference defaults to `wepiqx/MiMo-V2.6-Distill-Qwen-9B-GGUF-MERNIK` and its 5.1 GB `5100.gguf` file. The dashboard's **Choose model** menu also offers `mradermacher/epstein-llama-3.2-3B-v2-GGUF` using its recommended 2.02 GB `Q4_K_M` quant, `mradermacher/MechaEpstein-8000-GGUF` using `MechaEpstein-8000.Q8_0.gguf` (8.7 GB), and a custom repository/file option. The MiMo preset retains Qwen thinking mode; the other presets use their model's default chat template. The worker runs `llama.cpp` on one L4 and scales to zero after 60 idle seconds. The Discord gateway stays local; GPU compute starts only when a response is requested. Model weights persist in a Modal Volume. At the current listed rate of $0.09/GiB-month, MiMo's 5.1 GB file is about $0.45/month and MechaEpstein Q8_0's 8.1 GiB file is about $0.73/month, before any free-storage allowance and before accounting for other cached models.

```powershell
$env:MODAL_MODEL_ID = "wepiqx/MiMo-V2.6-Distill-Qwen-9B-GGUF-MERNIK"
$env:MODAL_MODEL_FILE = "MiMo-V2.6-Distill-Qwen-9B-MERNIK-5100.gguf"
$env:MODAL_GPU = "L4"
$env:MODAL_MAX_MODEL_LEN = "65536"
```

The dashboard offers 40k, 64k, and 128k Modal context settings. Set the limit no higher than the model's native context: the MechaEpstein Q8_0 smoke test reported a 40,960-token training context, and llama.cpp caps larger settings to that value. Its current `.env` setting is 40,960 so bot-side context trimming matches the worker. The existing MiMo setup uses Q8 KV cache and retains its 64k default. Local context stays at 4k by default with visible warnings and oldest-turn trimming. L4 is the cost-efficient first choice for one-at-a-time chat; A10G costs more at current Modal rates with similar memory. H100 may reduce latency, but must be over four times faster to reduce GPU cost per response. The throughput example's 100% utilization is for batched offline work, not a single interactive Discord request; batching here could add chat latency.

1. Install the project dependencies locally and run `modal setup`.
2. Run `node dash.mjs` and use **Config** to choose the inference backend. The local Discord gateway uses the token in `.env`; the Modal inference worker does not need Discord credentials.
3. Use **Config** to select Modal inference and the context size. Use **Deploy Modal worker** once to deploy the scale-to-zero class; this does not start a GPU. Then use **Run bot locally** to keep the Discord gateway connected while inference is remote.
4. **Test Modal model** runs one real completion on L4 after an explicit confirmation. Once verified, choose **Run locally** to start the Discord gateway; messages use the configured backend.
5. **Follow Modal logs** mirrors worker output into `bot.log`. The worker scales down after 60 seconds idle.

The Modal model test is billed for cold start, download if uncached, generation, and the 60-second scale-down window. The Modal Volume keeps downloaded GGUF files between invocations, so subsequent starts avoid downloading them again. At the listed rate checked on 2026-10-01, L4 compute is $0.80/GPU-hour (about $0.013 per minute), before CPU and memory charges. Cold-start and model-load duration varies, so measure a real completion for a per-request estimate.

**Check Modal budget** displays workspace metered spend and remaining amount against the $30 planning budget, separate from billed cost after credits. This is a display, not a hard spending cap.

## Tests

```powershell
python -m unittest test_core -v
```
