"""/neuron: Qwen3.5-4B with single MLP neurons pushed, from the psychograph-neurons Modal app.

The worker lives in ml/mechinterp/neuron_worker.py (deploy it from there). A push is (layer, neuron, dose), 0-indexed,
with the dose in multiples of the layer's median residual norm. The named neurons come from that folder's zoo.json, read
live (the notebook saves new finds there); data/neurons.json is the copy the bot falls back on outside the hub.

A voice is a persona floor (S14, S21 in ml/mechinterp): the residual held at a thinker's in-character level across a
window of layers, lifting only the tokens that fall short. Voices come from voices.json the same way (hub, then data/).

A Rig is one prompt with its pushes and voice; the cog turns it into a message with dials and buttons.
"""

from __future__ import annotations

import base64
import json
import os
import re
import struct
import time
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

import discord

from .render import EMBED_COLOR

APP_NAME = "psychograph-neurons"
CLASS_NAME = "NeuronWorker"
HUB = Path(__file__).resolve().parents[2] / "ml" / "mechinterp"
DATA = Path(__file__).resolve().parents[1] / "data"
HUB_ZOO, BUNDLED_ZOO = HUB / "zoo.json", DATA / "neurons.json"
HUB_VOICES, BUNDLED_VOICES = HUB / "voices.json", DATA / "voices.json"
SITE = os.getenv("NEURON_SITE", "https://neurons.subsurfaces.net").rstrip("/")
LAYERS, WIDTH = 32, 9216
DEFAULT_DOSE = 0.8
MAX_DOSE = 1.5  # past this most neurons just break the model
MAX_PUSHES = 4
HOT_STACK = 2.0  # S8: pushes add, but their damage compounds; past about this in total most stacks break
MAX_NEW_TOKENS = 280
WARM_SECONDS = 270  # the worker idles 300 s before scaling to zero
PUSH_RE = re.compile(r"L?\s*(\d+)\s*[\s:/,]\s*N?\s*(\d+)(?:\s*[\s:@x]\s*([+-]?\d*\.?\d+))?", re.IGNORECASE)

Push = tuple[int, int, float]
_worker = None  # the deployed class, looked up once
_last_answer = 0.0  # monotonic time of the worker's last answer, to tell a warm GPU from a cold one


def _pick(env: str, hub: Path, bundled: Path) -> Path:
    if os.getenv(env):
        return Path(os.environ[env])
    return hub if hub.is_file() else bundled


def zoo_file() -> Path:
    """NEURON_ZOO_FILE if set, else the hub's zoo.json when the bot runs inside the Psychograph hub, else the copy."""
    return _pick("NEURON_ZOO_FILE", HUB_ZOO, BUNDLED_ZOO)


def voices_file() -> Path:
    return _pick("NEURON_VOICES_FILE", HUB_VOICES, BUNDLED_VOICES)


def _zoo_json(path: Path | None = None) -> dict:
    try:
        return json.loads((path or zoo_file()).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_zoo(path: Path | None = None) -> dict[str, list[Push]]:
    """Read on every call (it's a few kilobytes), so neurons saved from the notebook show up without a restart."""
    neurons = _zoo_json(path).get("neurons", {})
    return {name: [(int(layer), int(neuron), float(dose)) for layer, neuron, dose in pushes] for name, pushes in neurons.items()}


def zoo_notes(path: Path | None = None) -> dict[str, dict]:
    """Per zoo entry: group, what it does, where it came from."""
    return _zoo_json(path).get("notes", {})


def zoo_lookup(text: str) -> tuple[str, list[Push]] | None:
    """A zoo entry by name, ignoring case."""
    wanted = text.strip().casefold()
    return next(((name, pushes) for name, pushes in load_zoo().items() if name.casefold() == wanted), None)


def parse_pushes(text: str) -> list[Push]:
    """"L24 N3102 +0.8; L17 N1991" (or "24:3102:0.8") into pushes; a missing dose is +0.8."""
    pushes = []
    for part in filter(None, (piece.strip() for piece in re.split(r"[;|]|,(?=\s*L)", text, flags=re.IGNORECASE))):
        match = PUSH_RE.fullmatch(part)
        if not match:
            raise ValueError(f"Couldn't read `{part}`: write pushes like `L24 N3102 +0.8`.")
        layer, neuron = int(match[1]), int(match[2])
        dose = float(match[3]) if match[3] else DEFAULT_DOSE
        if not (0 <= layer < LAYERS and 0 <= neuron < WIDTH):
            raise ValueError(f"There's no L{layer} N{neuron}: layers go 0-{LAYERS - 1}, neurons 0-{WIDTH - 1}.")
        if abs(dose) > MAX_DOSE:
            raise ValueError(f"A dose of {dose:+g} is past ±{MAX_DOSE}, where the model just turns to mush.")
        pushes.append((layer, neuron, dose))
    if not pushes:
        raise ValueError("No pushes given: write them like `L24 N3102 +0.8`.")
    if len(pushes) > MAX_PUSHES:
        raise ValueError(f"At most {MAX_PUSHES} pushes at once.")
    return pushes


def label(pushes: list[Push]) -> str:
    return " + ".join(f"L{layer} N{neuron} {dose:+.1f}" for layer, neuron, dose in pushes) or "unsteered"


def command(pushes: list[Push]) -> str:
    """The pushes as /neuron takes them, for copying."""
    return "; ".join(f"L{layer} N{neuron} {dose:+g}" for layer, neuron, dose in pushes)


# Voices ------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Voice:
    name: str
    short: str
    window: tuple[int, int]
    floors: tuple[tuple[int, int, float], ...]  # (layer, index into the file's directions, tau)
    recipe: str  # "W": S14's whole-vector floor; "W+S": plus a floor on what's only this thinker's (S21)


@lru_cache(maxsize=4)
def _voices(path: Path, stamp: float) -> tuple[dict[str, Voice], tuple[str, ...]]:
    """Parsed once per file version (the directions are ~1 MB of base64 float16)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    voices = {
        name: Voice(name, v["short"], tuple(v["window"]), tuple((int(a), int(b), float(c)) for a, b, c in v["floors"]),
                    v.get("recipe", data.get("recipe", "W")))
        for name, v in data["voices"].items()
    }
    return voices, tuple(data["directions"])


def load_voices(path: Path | None = None) -> dict[str, Voice]:
    path = path or voices_file()
    try:
        return _voices(path, path.stat().st_mtime)[0]
    except (OSError, ValueError, KeyError):
        return {}


def find_voice(text: str | None) -> Voice | None:
    if not text:
        return None
    wanted = text.strip().casefold()
    return next((v for v in load_voices().values() if wanted in (v.name.casefold(), v.short.casefold())), None)


def voice_request(voice: Voice, path: Path | None = None) -> tuple[list[list], list[list[float]]]:
    """The worker's floor pushes for one voice, and the directions they index (only the ones it uses)."""
    path = path or voices_file()
    encoded = _voices(path, path.stat().st_mtime)[1]
    pushes, directions, local = [], [], {}
    for layer, index, tau in voice.floors:
        if index not in local:
            local[index] = len(directions)
            raw = base64.b64decode(encoded[index])
            directions.append(list(struct.unpack(f"<{len(raw) // 2}e", raw)))
        pushes.append([layer, -(local[index] + 1), tau, "floor"])
    return pushes, directions


# Rigs --------------------------------------------------------------------------------------------------------------


@dataclass
class Rig:
    """One prompt with its pushes and voice, and what came back."""
    prompt: str
    pushes: list[Push] = field(default_factory=list)
    voice: Voice | None = None
    name: str | None = None  # the zoo entry or "Lucky dip" it started as
    owner: int = 0
    secret: str | None = None  # the dip's words, shown as a spoiler
    reply: str = ""
    plain: str | None = None  # the unsteered reply, once asked for
    takes: int = 0
    busy: bool = False

    def fork(self, owner: int) -> Rig:
        return replace(self, owner=owner, takes=0, busy=False, pushes=list(self.pushes))

    @property
    def title(self) -> str:
        parts = [self.name] if self.name else []
        if not self.name and self.pushes:
            parts.append(label(self.pushes))
        if self.voice:
            parts.insert(0, f"as {self.voice.short}")
        icon = "🎲" if self.secret else "🎙️" if self.voice and not self.pushes else "🧠"
        return f"{icon} " + (" · ".join(parts) or "unsteered")


def scaled(pushes: list[Push], factor: float) -> list[Push]:
    """Every dose times factor, to the nearest 0.05 and within ±MAX_DOSE; a dose never rounds to zero."""
    out = []
    for layer, neuron, dose in pushes:
        new = round(dose * factor / 0.05) * 0.05 or (0.05 if dose > 0 else -0.05)
        out.append((layer, neuron, round(max(-MAX_DOSE, min(MAX_DOSE, new)), 2)))
    return out


def flipped(pushes: list[Push]) -> list[Push]:
    return [(layer, neuron, -dose) for layer, neuron, dose in pushes]


LEVER_DOSE = 0.4  # S20: where sharp levers (lens z ≥ 10) most often stay coherent and on topic


def warnings(rig: Rig, glances: dict | None = None) -> list[str]:
    """What the studies say is likely to go wrong with this combination."""
    out = []
    hot = [push for push in rig.pushes if push[2] > LEVER_DOSE + 0.2 and (glance := (glances or {}).get(push[:2])) and glance.lens_z >= 10]
    if hot:
        names = ", ".join(f"L{layer} N{neuron}" for layer, neuron, _ in hot)
        out.append(f"⚡ {names}: sharp levers read best near +{LEVER_DOSE} (S20). Higher, they mostly break. Try 🔉.")
    total = sum(abs(dose) for _, _, dose in rig.pushes)
    if total > HOT_STACK:
        out.append(f"A hot stack ({total:.1f} in total): pushes add, but their damage compounds (S8). If it breaks, go softer.")
    layers = [layer for layer, _, _ in rig.pushes]
    if len(set(layers)) < len(layers):
        out.append("Two pushes share a layer, so they land on the same spot.")
    if rig.voice and rig.pushes:
        out.append("A voice plus neurons costs more coherence than either alone (S15). Heidegger takes it best.")
    return out


def dial(push: Push, words: list[str], lever: bool) -> str:
    """One push as a line: the neuron, a dose bar, and what that pole writes."""
    layer, neuron, dose = push
    cells = max(1, round(min(abs(dose), MAX_DOSE) / MAX_DOSE * 6))
    bar = "▰" * cells + "▱" * (6 - cells)
    writes = f" · writes *{', '.join(words)}*" if words else ""
    return f"`L{layer:<2} N{neuron:<4}` `{bar}` **{dose:+.2f}**{' ⚡' if lever and dose > 0 else ''}{writes}"


def bench_url(pushes: list[Push]) -> str:
    """The neuron's own page for a single push, else the site's bench with these pushes loaded."""
    if len(pushes) == 1:
        return f"{SITE}/#/n/{pushes[0][0]}/{pushes[0][1]}"
    return f"{SITE}/#/bench?p=" + ",".join(f"{layer}:{neuron}:{dose:g}" for layer, neuron, dose in pushes)


def warm() -> bool:
    return time.monotonic() - _last_answer < WARM_SECONDS


async def generate(rig: Rig, plain: bool = False) -> None:
    """Fill the rig's reply (and, with `plain`, the unsteered one) in one batch on the worker."""
    global _worker, _last_answer
    # Imported lazily: on Windows, importing modal switches the default event loop policy (see backends.py).
    import modal

    if _worker is None:
        _worker = modal.Cls.from_name(APP_NAME, CLASS_NAME)()
    pushes, directions = [list(push) for push in rig.pushes], []
    if rig.voice:
        floors, directions = voice_request(rig.voice)
        pushes += floors
    rows = [{"prompt": rig.prompt, "pushes": pushes}]
    if plain:
        rows.append({"prompt": rig.prompt, "pushes": []})
    replies = await _worker.generate.remote.aio(rows, max_new_tokens=MAX_NEW_TOKENS, temperature=0.7, seed=None,
                                               directions=directions or None)
    _last_answer = time.monotonic()
    rig.reply, rig.takes = replies[0]["text"], rig.takes + 1
    if plain:
        rig.plain = replies[1]["text"]


def _clip(text: str, limit: int) -> str:
    text = text.strip() or "*(nothing)*"
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def waiting(rig: Rig, note: str | None = None) -> discord.Embed:
    """The placeholder while the GPU works, honest about a cold start."""
    note = note or ("🧠 Thinking…" if warm() else "☁️ Waking the GPU, about a minute…")
    embed = discord.Embed(title=_clip(rig.title, 256), description=f"*{note}*", color=EMBED_COLOR)
    embed.set_footer(text=_clip(f"Qwen3.5-4B · “{rig.prompt}”", 2048))
    return embed


def render(rig: Rig, glances: dict | None = None) -> discord.Embed:
    """The rig as an embed. glances maps (layer, neuron) to the atlas's Glance, when the atlas answered."""
    glances = glances or {}
    url = bench_url(rig.pushes) if rig.pushes and not rig.secret else None
    embed = discord.Embed(title=_clip(rig.title, 256), url=url, description=_clip(rig.reply, 3000), color=EMBED_COLOR)
    lines = []
    for push in rig.pushes:
        glance = glances.get(push[:2])
        words = [] if rig.secret or glance is None else glance.words(push[2])
        lines.append(dial(push, words, bool(glance and glance.lever)))
    if rig.voice:
        span = f"layers {rig.voice.window[0]}–{rig.voice.window[1]}"
        how = "its shared voice and what's only theirs" if rig.voice.recipe == "W+S" else "their in-character level"
        lines.append(f"🎙️ **{rig.voice.short}**: held at {how}, {span}")
    if lines:
        embed.add_field(name="Dials", value=_clip("\n".join(lines), 1024), inline=False)
    if rig.secret:
        embed.add_field(name="What's it about?", value=f"||{rig.secret}||: a sharp lever, lens z ≥ 10. Guess before you peek.", inline=False)
    if rig.plain is not None:
        embed.add_field(name="Unsteered", value=_clip(rig.plain, 1024), inline=False)
    heads_up = warnings(rig, glances)
    if heads_up:
        embed.add_field(name="Heads up", value=_clip("\n".join(heads_up), 1024), inline=False)
    takes = f" · take {rig.takes}" if rig.takes > 1 else ""
    embed.set_footer(text=_clip(f"Qwen3.5-4B{takes} · “{rig.prompt}”", 2048))
    return embed
