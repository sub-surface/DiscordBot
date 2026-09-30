# Psychograph

A small Discord bot that responds in DMs, when mentioned, or when someone replies to it. Choose a persona per channel. Run inference locally with LM Studio or call an on-demand Modal model worker from the local bot.

## Commands

- `/persona [name]` shows or selects the channel persona.
- `/reset` clears conversation history for the channel.
- `/verbosity [concise|balanced|detailed]` shows or sets reply detail for the channel.
- `/model` shows the configured inference backend and model target; Modal model changes are made in the dashboard and require redeployment.
- `/cost` shows Modal workspace usage for the current month (Manage Server permission in guilds).
- `/status` summarizes channel settings and the configured model target.
- `/chess new`, `/chess move`, `/chess board`, and `/chess resign` manage a channel's chess game.

Chess also accepts moves when the channel's persona is `chess` and the bot is mentioned or replied to.
Chess moves are selected by local Stockfish on the bot's CPU; move generation uses neither LM Studio nor Modal. Install Stockfish separately and set `STOCKFISH_PATH` to its executable (or put it on `PATH`). `/chess commentary on` optionally adds a note using local LM Studio only; commentary defaults off and never uses Modal. `/chess commentary off` returns to CPU-only chess. If Stockfish is unavailable, the submitted move is rolled back.
The bot only handles messages and slash commands in `#sim-city`, `#little-st-james`, and `#chess`.

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

Local prompts are capped at 4k tokens by default, leaving output headroom. Older reply history is trimmed before generation, and the bot adds a warning when context is trimmed or nears the cap. Configure 2k/4k from the dashboard. Enable the Message Content Intent for the bot in the Discord Developer Portal. Conversation history stays in `history.db`.

On Windows, Modal 1.6.0 emits a non-fatal deprecation warning under Python 3.14 for its event-loop compatibility policy. At a convenient maintenance point, recreate the virtual environment with the documented Python 3.12, reinstall requirements, and run the tests; do not patch files in `site-packages`.

## Modal

Modal inference defaults to `wepiqx/MiMo-V2.6-Distill-Qwen-9B-GGUF-MERNIK` and its 5.1 GB `5100.gguf` file. The dashboard's **Choose model** menu also offers `mradermacher/epstein-llama-3.2-3B-v2-GGUF` using its recommended 2.02 GB `Q4_K_M` quant, plus a custom repository/file option. The Llama preset uses its native chat template; the MiMo preset retains Qwen thinking mode. The worker runs `llama.cpp` on one L4 and scales to zero after 60 idle seconds. The Discord gateway stays local; GPU compute starts only when a response is requested. Model weights persist in a Modal Volume, costing roughly $0.45/month before GPU use.

```powershell
$env:MODAL_MODEL_ID = "wepiqx/MiMo-V2.6-Distill-Qwen-9B-GGUF-MERNIK"
$env:MODAL_MODEL_FILE = "MiMo-V2.6-Distill-Qwen-9B-MERNIK-5100.gguf"
$env:MODAL_GPU = "L4"
$env:MODAL_MAX_MODEL_LEN = "65536"
```

The worker supports 64k or 128k context, selected under **Config** in the dashboard. The GGUF is about 5 GiB, and Qwen3.5's hybrid architecture limits full-attention KV cache; 128k with Q8 KV cache is a reasonable fit on L4, but run the smoke test before choosing it. Local context stays at 4k by default with visible warnings and oldest-turn trimming. L4 is the cost-efficient first choice for one-at-a-time chat; A10G costs more at current Modal rates with similar memory. H100 may reduce latency, but must be over four times faster to reduce GPU cost per response. The throughput example's 100% utilization is for batched offline work, not a single interactive Discord request; batching here could add chat latency.

1. Install the project dependencies locally and run `modal setup`.
2. Run `node dash.mjs` and use **Config** to choose the inference backend. The local Discord gateway uses the token in `.env`; the Modal inference worker does not need Discord credentials.
3. Use **Config** to select Modal inference and the context size. Use **Deploy Modal worker** once to deploy the scale-to-zero class; this does not start a GPU. Then use **Run bot locally** to keep the Discord gateway connected while inference is remote.
4. **Test Modal model** runs one real completion on L4 after an explicit confirmation. Once verified, choose **Run locally** to start the Discord gateway; messages use the configured backend.
5. **Follow Modal logs** mirrors worker output into `bot.log`. The worker scales down after 60 seconds idle.

The Modal model test is billed for cold start, download if uncached, and generation. The Modal Volume keeps the ~5 GiB GGUF between invocations, so subsequent starts avoid downloading it again.

**Check Modal budget** displays workspace metered spend and remaining amount against the $30 planning budget, separate from billed cost after credits. This is a display, not a hard spending cap.

## Tests

```powershell
python -m unittest test_core -v
```
