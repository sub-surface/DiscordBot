from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import modal

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


PRESET = _model_preset()
MODEL_ALIAS = "psychograph-model"
GPU = os.getenv("MODAL_GPU", "L4")
MAX_MODEL_LEN = int(os.getenv("MODAL_MAX_MODEL_LEN") or PRESET.get("context", 65536))
SCALEDOWN_SECONDS = int(os.getenv("MODAL_SCALEDOWN_SECONDS", "60"))
ENABLE_THINKING = (
    os.getenv("MODAL_ENABLE_THINKING") or str(PRESET.get("thinking", True))
).lower() in {"1", "true", "yes", "on"}
MODEL_CACHE_DIR = "/root/.cache/huggingface"
SERVER_PORT = 8080

app = modal.App(APP_NAME)
model_cache = modal.Volume.from_name("psychograph-model-cache", create_if_missing=True)
image = (
    modal.Image.from_registry("ghcr.io/ggml-org/llama.cpp:server-cuda", add_python="3.12")
    .entrypoint([])
    .pip_install("huggingface_hub[hf_xet]", "openai")
)


@app.cls(
    image=image,
    gpu=GPU,
    env={
        "MODAL_MODEL_ID": MODEL_REPO,
        "MODAL_MODEL_FILE": MODEL_FILE,
        "MODAL_GPU": GPU,
        "MODAL_MAX_MODEL_LEN": str(MAX_MODEL_LEN),
        "MODAL_SCALEDOWN_SECONDS": str(SCALEDOWN_SECONDS),
        "MODAL_ENABLE_THINKING": str(ENABLE_THINKING).lower(),
    },
    min_containers=0,
    max_containers=1,
    scaledown_window=SCALEDOWN_SECONDS,
    startup_timeout=1200,
    timeout=1200,
    volumes={MODEL_CACHE_DIR: model_cache},
)
class MimoWorker:
    @modal.enter()
    def start(self) -> None:
        from huggingface_hub import hf_hub_download

        self.model_path = hf_hub_download(
            repo_id=MODEL_REPO,
            filename=MODEL_FILE,
            cache_dir=MODEL_CACHE_DIR,
        )
        model_cache.commit()
        self.server = subprocess.Popen(
            [
                "/app/llama-server",
                "--model",
                self.model_path,
                "--host",
                "127.0.0.1",
                "--port",
                str(SERVER_PORT),
                "--alias",
                MODEL_ALIAS,
                "--api-key",
                "local-worker",
                "--ctx-size",
                str(MAX_MODEL_LEN),
                "--n-gpu-layers",
                "all",
                "--flash-attn",
                "on",
                "--cache-type-k",
                "q8_0",
                "--cache-type-v",
                "q8_0",
                "--parallel",
                "1",
                "--jinja",
            ],
        )
        self._wait_until_ready()

    def _wait_until_ready(self) -> None:
        from urllib.error import URLError
        from urllib.request import urlopen

        deadline = time.monotonic() + 900
        while time.monotonic() < deadline:
            if self.server.poll() is not None:
                raise RuntimeError("llama-server exited before becoming ready")
            try:
                with urlopen(f"http://127.0.0.1:{SERVER_PORT}/health", timeout=3):
                    return
            except (OSError, URLError):
                time.sleep(2)
        raise TimeoutError("llama-server did not become ready")

    @modal.method()
    def complete(
        self,
        messages: list[dict],
        _model: str,
        max_tokens: int,
        temperature: float,
        top_p: float,
    ) -> dict[str, str | int | float | None]:
        from openai import OpenAI

        client = OpenAI(
            base_url=f"http://127.0.0.1:{SERVER_PORT}/v1",
            api_key="local-worker",
            timeout=900,
            max_retries=0,
        )
        try:
            request_options = {}
            if ENABLE_THINKING:
                request_options["extra_body"] = {"chat_template_kwargs": {"enable_thinking": True}}
            started = time.perf_counter()
            result = client.chat.completions.create(
                model=MODEL_ALIAS,
                messages=messages,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                **request_options,
            )
            generation_seconds = time.perf_counter() - started
            choice = result.choices[0].message
            usage = result.usage
            response_metadata = getattr(result, "model_extra", None) or {}
            timings = response_metadata.get("timings", {})
            if not isinstance(timings, dict):
                timings = {}
            predicted_tokens = timings.get("predicted_n")
            completion_tokens = (
                predicted_tokens if isinstance(predicted_tokens, int)
                else usage.completion_tokens if usage else None
            )
            return {
                "text": (choice.content or "").strip(),
                "prompt_tokens": usage.prompt_tokens if usage else None,
                "completion_tokens": completion_tokens,
                "generation_seconds": generation_seconds,
                "eval_seconds": timings.get("predicted_ms", 0) / 1000 if timings.get("predicted_ms") is not None else None,
                "tokens_per_second": timings.get("predicted_per_second"),
            }
        finally:
            client.close()

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
