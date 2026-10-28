"""/neuron-card: what one Qwen3.5-4B neuron reads and writes, from the precomputed atlas on neurons.subsurfaces.net.

No GPU: the site's static files (made by ml/mechinterp/export_site.py; the byte layout is documented there) are fetched
once and kept in memory. Meta, vocab (~2.6 MB) and corpus (~4 MB) stay; layer files (~1.8 MB each) are kept for the
last few layers asked about. For /neuron's dials, the lens index (~7 MB: 3 tokens each way for every neuron), the
weights scan (~1.8 MB: lens z) and the saved replies (~1.5 MB) are fetched once too, so any neuron can be labelled
without a layer file.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import re
import struct
from collections import OrderedDict
from dataclasses import dataclass

import aiohttp
import discord

from .render import EMBED_COLOR

SITE = os.getenv("NEURON_SITE", "https://neurons.subsurfaces.net").rstrip("/")
TIMEOUT_SECONDS = 30
LAYER_CACHE = 6
NONE = 0xFFFFFFFF
LEVER_Z = 7.6  # S17: above this lens z (the top tenth of neurons) a push's own words turn up in about half its replies
DIP_Z = 10.0  # the lucky dip draws from the sharpest levers, as S18 did
WORD = re.compile(r"[A-Za-z]{4,}")


@dataclass
class Glance:
    """What a dial needs: the first lens tokens each way, and how sharp the + lens is (S17's lens z)."""
    up: list[str]
    down: list[str]
    lens_z: float

    def words(self, sign: float, limit: int = 3) -> list[str]:
        """The pole's lens tokens that read as words, for a one-line label."""
        return [token.strip() for token in (self.up if sign > 0 else self.down) if WORD.fullmatch(token.strip())][:limit]

    @property
    def lever(self) -> bool:
        return self.lens_z >= LEVER_Z


@dataclass
class NeuronCard:
    layer: int
    neuron: int
    peaks: tuple[float, float]  # strongest + and − activation (the − one as a magnitude)
    mean: float
    sd: float
    lens_up: list[str]
    lens_down: list[str]
    contexts: tuple[list[tuple[float, str]], list[tuple[float, str]]]  # per pole: (value, context with the hit in **)


def neuron_slices(buf: bytes, neuron: int, width: int, k: int) -> dict:
    """One neuron's rows out of a layer file: values, positions, lens tokens and stats."""
    base = width * 2 * k
    return {
        "values": struct.unpack_from(f"<{2 * k}e", buf, neuron * 2 * k * 2),
        "positions": struct.unpack_from(f"<{2 * k}I", buf, base * 2 + neuron * 2 * k * 4),
        "up": struct.unpack_from(f"<{k}I", buf, base * 6 + neuron * k * 4),
        "down": struct.unpack_from(f"<{k}I", buf, base * 6 + width * k * 4 + neuron * k * 4),
        "mean": struct.unpack_from("<f", buf, base * 6 + width * k * 8 + neuron * 4)[0],
        "sd": struct.unpack_from("<f", buf, base * 6 + width * k * 8 + width * 4 + neuron * 4)[0],
    }


def context(corpus: bytes, docs: tuple[int, ...], vocab: list[str], position: int, before: int = 14, after: int = 5) -> str:
    """The text around one corpus position, with the token itself wrapped in ** (Discord markdown escaped)."""
    lo, hi = 0, len(docs) - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if docs[mid] <= position:
            lo = mid
        else:
            hi = mid
    start, end = max(docs[lo], position - before), min(docs[lo + 1], position + after + 1)
    tokens = struct.unpack_from(f"<{end - start}I", corpus, start * 4)
    pieces = []
    for at, token in zip(range(start, end), tokens):
        text = discord.utils.escape_markdown(vocab[token]).replace("\n", " ⏎ ")
        pieces.append(f"**{text.strip() or '∅'}**" if at == position else text)
    return ("…" if start > docs[lo] else "") + "".join(pieces).strip() + ("…" if end < docs[lo + 1] else "")


class AtlasSite:
    def __init__(self, base: str = SITE) -> None:
        self.base = base
        self.meta: dict | None = None
        self.vocab: list[str] | None = None
        self.corpus: bytes | None = None
        self.docs: tuple[int, ...] | None = None
        self.layers: OrderedDict[int, bytes] = OrderedDict()
        self.index: bytes | None = None  # lens_index.bin
        self.scan: bytes | None = None  # scan.bin
        self.saved: dict | None = None  # replies.json
        self.pool: list[tuple[int, int]] | None = None  # the lucky dip's levers
        self._lock = asyncio.Lock()

    async def _fetch(self, session: aiohttp.ClientSession, path: str) -> bytes:
        async with session.get(f"{self.base}/data/{path}") as response:
            response.raise_for_status()
            return await response.read()

    def _session(self) -> aiohttp.ClientSession:
        return aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS))

    async def _basics(self, session: aiohttp.ClientSession) -> dict:
        if self.meta is None:
            self.meta = json.loads(await self._fetch(session, "meta.json"))
        if self.vocab is None:
            self.vocab = json.loads(await self._fetch(session, "vocab.json"))
        return self.meta

    async def _indexes(self) -> dict:
        async with self._lock, self._session() as session:
            meta = await self._basics(session)
            if self.index is None:
                self.index = await self._fetch(session, "lens_index.bin")
                self.scan = await self._fetch(session, "scan.bin")
        return meta

    async def glance(self, layer: int, neuron: int) -> Glance:
        meta = await self._indexes()
        at = layer * meta["neurons"] + neuron
        tokens = struct.unpack_from("<6I", self.index, at * 24)
        lens_z = struct.unpack_from("<e", self.scan, (at * 3 + 2) * 2)[0]
        return Glance([self.vocab[t] for t in tokens[:3]], [self.vocab[t] for t in tokens[3:]], float(lens_z))

    async def replies(self, layer: int, neuron: int) -> list[dict]:
        """Saved replies to pushes of this neuron from the studies: {s: study, q: prompt, d: dose, t: text, tag}."""
        async with self._lock, self._session() as session:
            if self.saved is None:
                self.saved = json.loads(await self._fetch(session, "replies.json"))
        return self.saved.get(f"{layer}/{neuron}", [])

    async def dip(self, rng: random.Random | None = None) -> tuple[int, int, Glance]:
        """A random sharp lever (lens z ≥ DIP_Z) whose top token is a plain word, layers 4 and up."""
        meta = await self._indexes()
        if self.pool is None:
            count, width = meta["layers"], meta["neurons"]
            self.pool = []
            for at in range(4 * width, count * width):
                if struct.unpack_from("<e", self.scan, (at * 3 + 2) * 2)[0] >= DIP_Z:
                    if WORD.fullmatch(self.vocab[struct.unpack_from("<I", self.index, at * 24)[0]].strip()):
                        self.pool.append(divmod(at, width))
        layer, neuron = (rng or random).choice(self.pool)
        return layer, neuron, await self.glance(layer, neuron)

    async def card(self, layer: int, neuron: int) -> NeuronCard:
        async with self._lock, self._session() as session:
            meta = await self._basics(session)
            if not (0 <= layer < meta["layers"] and 0 <= neuron < meta["neurons"]):
                raise ValueError(f"Layers go 0–{meta['layers'] - 1} and neurons 0–{meta['neurons'] - 1}.")
            if self.corpus is None:
                self.corpus = await self._fetch(session, "corpus.bin")
                raw = await self._fetch(session, "doc_offsets.bin")
                self.docs = struct.unpack(f"<{len(raw) // 4}i", raw)
            if layer not in self.layers:
                self.layers[layer] = await self._fetch(session, f"layers/L{layer}.bin")
                while len(self.layers) > LAYER_CACHE:
                    self.layers.popitem(last=False)
            self.layers.move_to_end(layer)
        k = meta["k"]
        row = neuron_slices(self.layers[layer], neuron, meta["neurons"], k)
        poles = []
        for pole in (0, 1):
            shown = []
            for value, position in zip(row["values"][pole * k:(pole + 1) * k], row["positions"][pole * k:(pole + 1) * k]):
                if position != NONE and value > 0:
                    shown.append((value, context(self.corpus, self.docs, self.vocab, position)))
            poles.append(shown)
        return NeuronCard(
            layer, neuron, (row["values"][0], row["values"][k]), row["mean"], row["sd"],
            [self.vocab[t] for t in row["up"]], [self.vocab[t] for t in row["down"]], (poles[0], poles[1]),
        )


def _chips(tokens: list[str], limit: int = 8) -> str:
    shown = [f"`{token.replace('`', 'ˋ').replace(chr(10), '⏎')}`" for token in tokens[:limit] if token.strip()]
    return " ".join(shown) or "—"


def _contexts(rows: list[tuple[float, str]], sign: str, limit: int = 3) -> str:
    lines = [f"{sign}{value:.2f} · {text}" for value, text in rows[:limit]]
    out = "\n".join(lines) or "Never fired on this side."
    return out if len(out) <= 1024 else out[:1021] + "…"


def lever_line(lens_z: float) -> str:
    if lens_z >= LEVER_Z:
        return f"⚡ lens z {lens_z:.1f}: a lever. Its words tend to turn up in what it writes when pushed (S17)"
    return f"lens z {lens_z:.1f}: a diffuse lens, so a push shifts tone more than topic"


def _saved(rows: list[dict], limit: int = 2) -> str:
    """A couple of real replies from the studies, the most telling first (a rated frame or tag, then the strongest dose)."""
    rows = sorted(rows, key=lambda r: (not r.get("tag"), -abs(r["d"])))[:limit]
    lines = []
    for r in rows:
        text = discord.utils.escape_markdown(" ".join(r["t"].split()))
        tag = f" · *{r['tag']}*" if r.get("tag") else ""
        lines.append(f"**{r['s']}** {r['d']:+.1f} · “{r['q']}”{tag}\n> {text[:300] + ('…' if len(text) > 300 else '')}")
    out = "\n".join(lines)
    return out if len(out) <= 1024 else out[:1021] + "…"


def render_card(card: NeuronCard, zoo_names: list[str], lens_z: float | None = None, saved: list[dict] | None = None) -> discord.Embed:
    title = f"L{card.layer} N{card.neuron}" + (f" · zoo: {', '.join(zoo_names)}" if zoo_names else "")
    embed = discord.Embed(title=title[:256], url=f"{SITE}/#/n/{card.layer}/{card.neuron}", color=EMBED_COLOR)
    embed.description = f"peak **+{card.peaks[0]:.2f}** / **−{card.peaks[1]:.2f}** · mean {card.mean:+.3f} · sd {card.sd:.3f}"
    if lens_z is not None:
        embed.description += f"\n{lever_line(lens_z)}"
    embed.add_field(name="+ push promotes", value=_chips(card.lens_up), inline=True)
    embed.add_field(name="+ push suppresses", value=_chips(card.lens_down), inline=True)
    embed.add_field(name="Fires most (+)", value=_contexts(card.contexts[0], "+"), inline=False)
    embed.add_field(name="Fires most (−)", value=_contexts(card.contexts[1], "−"), inline=False)
    if saved:
        embed.add_field(name=f"What pushing it did ({len(saved)} saved)", value=_saved(saved), inline=False)
    embed.set_footer(text="Precomputed atlas, no GPU · push it with the buttons, or /neuron ask")
    return embed


site = AtlasSite()
