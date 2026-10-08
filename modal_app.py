from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import modal

CONTAINER_STARTED = time.time()  # module import ≈ container start, for the boot timing the worker reports

APP_NAME = "psychograph"
MODEL_REPO = os.getenv(
    "MODAL_MODEL_ID", "wepiqx/MiMo-V2.6-Distill-Qwen-9B-GGUF-MERNIK"
)
MODEL_FILE = os.getenv(
    "MODAL_MODEL_FILE", "MiMo-V2.6-Distill-Qwen-9B-MERNIK-5100.gguf"
)


def _model_preset() -> dict:
    """Deploy defaults for this model from models.json (absent inside the container, where env is set)."""
    try:
        with open(Path(__file__).with_name("models.json"), encoding="utf-8") as presets:
            models = json.load(presets).get("models", [])
    except (OSError, ValueError):
        return {}
    model = f"{MODEL_REPO}/{MODEL_FILE}".casefold()
    for entry in models:
        if any(token.casefold() in model for token in entry.get("match", [])):
            return entry.get("modal", {})
    return {}


def _flag(name: str, default: object) -> bool:
    return (os.getenv(name) or str(default)).lower() in {"1", "true", "yes", "on"}


PRESET = _model_preset()
MODEL_ALIAS = "psychograph-model"
GPU = os.getenv("MODAL_GPU", "L4")
# 32k is ~5x the largest prompt the bot has sent; the bot trims to whatever this is, and a smaller KV cache
# leaves VRAM headroom (it's what lets the model fit a 16 GB T4).
MAX_MODEL_LEN = int(os.getenv("MODAL_MAX_MODEL_LEN") or PRESET.get("context", 32768))
# Idle seconds before scaling to zero. Every idle second is billed (Modal adds ~45 s of teardown on top),
# and most of the bot's GPU bill is this tail, so it's kept short until real cold-start data says otherwise.
SCALEDOWN_SECONDS = int(os.getenv("MODAL_SCALEDOWN_SECONDS", "60"))
# Whether the model has a thinking mode at all; the bot decides per request whether to use it (/bot thinking).
CAN_THINK = _flag("MODAL_ENABLE_THINKING", PRESET.get("thinking", True))
# Alpha: restore llama-server, weights already in VRAM, from a GPU memory snapshot instead of loading it.
GPU_SNAPSHOT = _flag("MODAL_GPU_SNAPSHOT", False)
MODEL_CACHE_DIR = "/root/.cache/huggingface"
SERVER_PORT = 8080
SERVER_URL = f"http://127.0.0.1:{SERVER_PORT}"
API_KEY = "local-worker"
# Pinned by digest so the llama.cpp build only changes on purpose (the floating tag is cached by Modal anyway).
LLAMA_IMAGE = "ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:fff6185edd2fbc4093aa5970bf6db53ed11c283e3a1c52a3e244cf27047264f4"

app = modal.App(APP_NAME)
model_cache = modal.Volume.from_name("psychograph-model-cache", create_if_missing=True)
image = (
    modal.Image.from_registry(LLAMA_IMAGE, add_python="3.12")
    .entrypoint([])
    .pip_install("huggingface_hub[hf_xet]==2.1.1")
)


def _post(path: str, body: dict, timeout: float) -> dict:
    from urllib.request import Request, urlopen

    request = Request(
        f"{SERVER_URL}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {API_KEY}"},
    )
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


@app.cls(
    image=image,
    gpu=GPU,
    env={
        "MODAL_MODEL_ID": MODEL_REPO,
        "MODAL_MODEL_FILE": MODEL_FILE,
        "MODAL_GPU": GPU,
        "MODAL_MAX_MODEL_LEN": str(MAX_MODEL_LEN),
        "MODAL_SCALEDOWN_SECONDS": str(SCALEDOWN_SECONDS),
        "MODAL_ENABLE_THINKING": str(CAN_THINK).lower(),
        "MODAL_GPU_SNAPSHOT": str(GPU_SNAPSHOT).lower(),
    },
    min_containers=0,
    max_containers=1,
    scaledown_window=SCALEDOWN_SECONDS,
    startup_timeout=1200,
    timeout=1200,
    volumes={MODEL_CACHE_DIR: model_cache},
    enable_memory_snapshot=GPU_SNAPSHOT,
    experimental_options={"enable_gpu_snapshot": True} if GPU_SNAPSHOT else None,
)
class MimoWorker:
    @modal.enter(snap=GPU_SNAPSHOT)
    def start(self) -> None:
        from huggingface_hub import hf_hub_download, try_to_load_from_cache

        began = time.monotonic()
        # The weights live on the Volume after the first boot: find them without asking the Hub, and only
        # download (and commit the Volume) when they're missing.
        cached = try_to_load_from_cache(MODEL_REPO, MODEL_FILE, cache_dir=MODEL_CACHE_DIR)
        if isinstance(cached, str):
            self.model_path = cached
        else:
            self.model_path = hf_hub_download(repo_id=MODEL_REPO, filename=MODEL_FILE, cache_dir=MODEL_CACHE_DIR)
            model_cache.commit()
        located = time.monotonic()
        self.server = subprocess.Popen(
            [
                "/app/llama-server",
                "--model", self.model_path,
                "--host", "127.0.0.1",
                "--port", str(SERVER_PORT),
                "--alias", MODEL_ALIAS,
                "--api-key", API_KEY,
                "--ctx-size", str(MAX_MODEL_LEN),
                "--n-gpu-layers", "all",
                "--flash-attn", "on",
                "--cache-type-k", "q8_0",
                "--cache-type-v", "q8_0",
                "--parallel", "1",
                "--jinja",
            ],
        )
        self._wait_until_ready()
        ready = time.monotonic()
        self.boot = {
            "container_seconds": round(time.time() - CONTAINER_STARTED, 2),  # container start → server ready
            "locate_seconds": round(located - began, 2),
            "load_seconds": round(ready - located, 2),
        }
        self.served = 0
        print(f"boot: {json.dumps(self.boot)} gpu={GPU} ctx={MAX_MODEL_LEN} snapshot={GPU_SNAPSHOT}", flush=True)

    @modal.enter(snap=False)
    def restored(self) -> None:
        """After a snapshot restore the timings above are the snapshot's; mark the boot as a restore."""
        if GPU_SNAPSHOT:
            self._wait_until_ready(timeout=120)
            self.boot = {**self.boot, "restored": True}
            self.served = 0

    def _wait_until_ready(self, timeout: float = 900) -> None:
        from urllib.error import URLError
        from urllib.request import urlopen

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                raise RuntimeError("llama-server exited before becoming ready")
            try:
                with urlopen(f"{SERVER_URL}/health", timeout=3):
                    return
            except (OSError, URLError):
                time.sleep(0.25)
        raise TimeoutError("llama-server did not become ready")

    @modal.method()
    def complete(
        self,
        messages: list[dict],
        _model: str,
        max_tokens: int,
        temperature: float,
        top_p: float,
        options: dict | None = None,
    ) -> dict[str, object]:
        """One chat completion. `options`: thinking (bool), top_k (int). The reply carries the worker's own
        state, so the bot can record real cold starts and boot times rather than guessing them."""
        options = options or {}
        body: dict = {
            "model": MODEL_ALIAS,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "top_p": top_p,
            # Sent either way: Qwen-style templates think by default unless told not to.
            "chat_template_kwargs": {"enable_thinking": CAN_THINK and bool(options.get("thinking", CAN_THINK))},
        }
        if options.get("top_k"):
            body["top_k"] = int(options["top_k"])
        cold = self.served == 0
        self.served += 1
        started = time.perf_counter()
        result = _post("/v1/chat/completions", body, timeout=900)
        generation_seconds = time.perf_counter() - started
        choice = (result.get("choices") or [{}])[0].get("message") or {}
        usage = result.get("usage") or {}
        timings = result.get("timings") if isinstance(result.get("timings"), dict) else {}
        predicted = timings.get("predicted_n")
        return {
            "text": (choice.get("content") or "").strip(),
            "prompt_tokens": usage.get("prompt_tokens"),
            "prompt_processed": timings.get("prompt_n"),  # after prompt-cache reuse
            "completion_tokens": predicted if isinstance(predicted, int) else usage.get("completion_tokens"),
            "reasoning_chars": len(choice.get("reasoning_content") or ""),
            "generation_seconds": generation_seconds,
            "eval_seconds": timings["predicted_ms"] / 1000 if timings.get("predicted_ms") is not None else None,
            "prompt_seconds": timings["prompt_ms"] / 1000 if timings.get("prompt_ms") is not None else None,
            "tokens_per_second": timings.get("predicted_per_second"),
            "cold": cold,
            "boot": self.boot if cold else None,
            "worker": {"gpu": GPU, "context": MAX_MODEL_LEN, "scaledown": SCALEDOWN_SECONDS, "thinking": body["chat_template_kwargs"]["enable_thinking"]},
        }

    @modal.exit()
    def stop(self) -> None:
        if getattr(self, "server", None) and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.server.kill()


@app.local_entrypoint()
def smoke() -> None:
    response = MimoWorker().complete.remote(
        [{"role": "user", "content": "Reply in one sentence: the on-demand Modal model is running."}],
        MODEL_ALIAS,
        128,
        1.0,
        0.95,
    )
    print(response)


# A fixed workload for comparing GPUs and flags: `MODAL_GPU=T4 modal run modal_app.py::bench`. One cold boot,
# then a short chat turn and a long channel-context turn, thinking off, so runs are comparable. ~1-2 GPU-minutes.
BENCH_FILLER = (
    "Santi: did anyone see the thing about the new stadium\n"
    "Zack: it's a money pit, every city falls for it\n"
    "Lizzie: the renderings looked nice though, admit it\n"
)


@app.local_entrypoint()
def bench(rounds: int = 2) -> None:
    worker = MimoWorker()
    long_context = BENCH_FILLER * 60  # ≈ 2.5k tokens, the bot's typical chat prompt
    print(f"gpu={GPU} ctx={MAX_MODEL_LEN} snapshot={GPU_SNAPSHOT}")
    for round_number in range(rounds):
        # The round number leads each prompt so llama-server's prompt cache can't skip the prefill.
        cases = {
            "short": [{"role": "user", "content": f"[{round_number}] Write two sentences about why stadiums are a bad public investment."}],
            "long": [
                {"role": "system", "content": f"[{round_number}] You are a sarcastic Discord regular. Reply in under 120 words."},
                {"role": "user", "content": f"[Recent channel messages]\n{long_context}\n[Reply to this]\nSanti: thoughts?"},
            ],
        }
        for name, messages in cases.items():
            started = time.perf_counter()
            result = worker.complete.remote(messages, MODEL_ALIAS, 256, 0.7, 0.95, {"thinking": False, "top_k": 20})
            wall = time.perf_counter() - started
            processed = result.get("prompt_processed") or 0
            prompt_rate = processed / result["prompt_seconds"] if result.get("prompt_seconds") else 0
            print(
                f"round {round_number} {name:5}: wall {wall:5.1f}s · prompt {processed} tok @ {prompt_rate:6.0f} tok/s"
                f" · decode {result['completion_tokens']} tok @ {result['tokens_per_second'] or 0:5.1f} tok/s"
                + (f" · boot {result['boot']}" if result.get("boot") else "")
            )
