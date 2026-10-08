"""/santi-slop: the posts shared in #sim-city, ranked, as a card. No model, no GPU.

  collect   the channel's history is scanned once and then only from where it left off (plus the last two
            days again, to refresh reaction counts); every x.com / twitter / fxtwitter / fixupx / vxtwitter
            link is kept as a share: who posted it, when, and how many reactions it got here
  rank      each post's public numbers come from the FxEmbed API (cached for six hours) and are blended with
            how it did here: log(likes + 2·reposts + quotes) + ¼·log(views) + reactions and repeat shares
  rate      Jev reads the top few and says what each one is (news, hype, meme or slop) and how sloppy
  render    a dark card drawn with Pillow: rank, avatar, author, the post, its numbers and a slop meter
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING
from urllib.request import Request, urlopen

import discord

from .board import _get_font
from .conversation import TWEET_LINK_RE

if TYPE_CHECKING:
    from .bot import PsychographBot

log = logging.getLogger("psychograph.slop")

PERIODS = {"day": 1, "week": 7, "month": 30, "all": None}
STATS_TTL = 6 * 3600
RESCAN = timedelta(days=2)       # re-read this much of the recent history each time, for fresh reaction counts
SHOWN = 6                        # posts on the card
STATS_LIMIT = 120                # most posts to look up per report (the most reacted first)
KINDS = {
    "news": "Real news or an announcement: something actually happened.",
    "hype": "Hype: breathless claims, benchmarks, 'this changes everything'.",
    "meme": "A joke, meme or shitpost.",
    "slop": "Low-effort slop: engagement bait, AI-generated filler, grifting.",
}


@dataclass
class Post:
    status_id: str
    url: str                     # the post itself
    shared: list[dict]           # the shares here, oldest first
    name: str = ""
    handle: str = ""
    avatar_url: str = ""
    text: str = ""
    likes: int = 0
    reposts: int = 0
    quotes: int = 0
    views: int = 0
    kind: str = ""
    sloppiness: float | None = None
    avatar: bytes | None = field(default=None, repr=False)

    @property
    def reacts(self) -> int:
        return sum(share["reacts"] for share in self.shared)

    @property
    def score(self) -> float:
        reach = math.log10(1 + self.likes + 2 * self.reposts + self.quotes) + 0.25 * math.log10(1 + self.views)
        return reach + 0.8 * math.log2(1 + self.reacts) + 0.5 * (len(self.shared) - 1)

    @property
    def jump(self) -> str:
        first = self.shared[0]
        return f"https://discord.com/channels/{first['guild_id']}/{first['channel_id']}/{first['message_id']}"


# ── Collect ─────────────────────────────────────────────────────────

async def collect(bot: PsychographBot, channel: discord.TextChannel) -> None:
    """Record every post link shared in `channel` since the last scan (and the last two days again)."""
    key = f"slop_scan:{channel.id}"
    last = int(bot.store.state(key) or 0)
    recent = discord.utils.time_snowflake(datetime.now(timezone.utc) - RESCAN)
    after = discord.Object(min(last, recent)) if last else None
    newest = last
    async for message in channel.history(limit=None, after=after, oldest_first=True):
        newest = max(newest, message.id)
        if message.author.bot or message.webhook_id:
            continue
        ids = dict.fromkeys(match.group(1) for match in TWEET_LINK_RE.finditer(message.content))
        reacts = sum(reaction.count for reaction in message.reactions)
        for status_id in ids:
            bot.store.save_share(
                message.id, status_id, channel.guild.id, channel.id, message.author.id,
                message.author.display_name, message.created_at.timestamp(), reacts,
            )
    if newest:
        bot.store.set_state(key, str(newest))


# ── Rank ────────────────────────────────────────────────────────────

def _fetch(status_id: str) -> dict | None:
    request = Request(
        f"https://api.fxtwitter.com/2/status/{status_id}",
        headers={"Accept": "application/json", "User-Agent": "Psychograph/1.0"},
    )
    try:
        with urlopen(request, timeout=6) as response:
            payload = json.loads(response.read(262144))
    except (OSError, TimeoutError, ValueError):
        return None
    status = payload.get("status") if isinstance(payload, dict) else None
    if not isinstance(status, dict):
        return None
    author = status.get("author") or {}
    return {
        "name": author.get("name") or "",
        "handle": author.get("screen_name") or "",
        "avatar_url": author.get("avatar_url") or "",
        "url": status.get("url") or f"https://x.com/i/status/{status_id}",
        "text": status.get("text") or "",
        **{key: int(status.get(key) or 0) for key in ("likes", "reposts", "quotes", "views")},
    }


async def _stats(bot: PsychographBot, status_id: str, gate: asyncio.Semaphore) -> dict | None:
    now = time.time()
    cached = bot.store.tweet_stats(status_id, STATS_TTL, now)
    if cached:
        return json.loads(cached)
    async with gate:
        data = await asyncio.to_thread(_fetch, status_id)
    if data:
        bot.store.save_tweet_stats(status_id, json.dumps(data), now)
    return data


def _download(url: str) -> bytes | None:
    try:
        with urlopen(Request(url, headers={"User-Agent": "Psychograph/1.0"}), timeout=6) as response:
            return response.read(1_000_000)
    except (OSError, TimeoutError, ValueError):
        return None


async def rank(bot: PsychographBot, channel_id: int, days: int | None) -> tuple[list[Post], list[dict]]:
    """(the posts ranked best first, every share in the period)."""
    since = time.time() - days * 86400 if days else 0.0
    shares = bot.store.shares(channel_id, since)
    grouped: dict[str, list[dict]] = {}
    for share in shares:
        grouped.setdefault(share["status_id"], []).append(share)
    candidates = sorted(grouped.items(), key=lambda item: -sum(s["reacts"] for s in item[1]))[:STATS_LIMIT]
    gate = asyncio.Semaphore(8)
    found = await asyncio.gather(*(_stats(bot, status_id, gate) for status_id, _ in candidates))
    posts = []
    for (status_id, group), data in zip(candidates, found):
        if not data:
            continue  # deleted, private or unreachable
        posts.append(Post(status_id, data.pop("url"), group, **data))
    posts.sort(key=lambda post: post.score, reverse=True)
    return posts, shares


async def rate(bot: PsychographBot, posts: list[Post]) -> None:
    """Jev's read of each post: what kind it is, and how sloppy (fails soft: no meter)."""

    async def one(post: Post) -> None:
        answers = await bot.jev.ask(
            {"post": f"{post.name} (@{post.handle}): {post.text[:600]}"},
            {
                "kind": {"type": "choice", "instructions": "What kind of post is `post`?", "criteria": KINDS},
                "slop": {"type": "noul", "instructions": "Is `post` slop: low-effort, engagement bait, hype or filler?"},
            },
        )
        if answers:
            post.kind = (answers.get("kind") or {}).get("choice", "")
            post.sloppiness = (answers.get("slop") or {}).get("noul")

    async def avatar(post: Post) -> None:
        if post.avatar_url:
            post.avatar = await asyncio.to_thread(_download, post.avatar_url)

    await asyncio.gather(*(one(post) for post in posts), *(avatar(post) for post in posts))


# ── Render ──────────────────────────────────────────────────────────

W, PAD, ROW, HEAD, FOOT = 1100, 44, 168, 214, 78
BG, PANEL, LINE = (12, 14, 19), (21, 24, 32), (34, 38, 50)
TEXT, MUTED, DIM = (236, 239, 244), (142, 150, 166), (88, 95, 110)
SLIME = (150, 232, 92)
KIND_COLORS = {"news": (96, 165, 250), "hype": (251, 146, 60), "meme": (192, 132, 252), "slop": SLIME}
EMOJI = re.compile("[\U00010000-\U0010FFFF☀-➿️‍]")


def _fonts() -> dict:
    face = lambda names, size: _get_font(names, size)  # noqa: E731
    return {
        "kicker": face(["seguisb.ttf", "DejaVuSans-Bold.ttf"], 17),
        "title": face(["seguibl.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"], 60),
        "sub": face(["segoeui.ttf", "DejaVuSans.ttf"], 22),
        "chip": face(["seguisb.ttf", "DejaVuSans-Bold.ttf"], 18),
        "rank": face(["seguibl.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"], 44),
        "name": face(["segoeuib.ttf", "DejaVuSans-Bold.ttf"], 24),
        "handle": face(["segoeui.ttf", "DejaVuSans.ttf"], 20),
        "body": face(["segoeui.ttf", "DejaVuSans.ttf"], 22),
        "stats": face(["seguisb.ttf", "DejaVuSans.ttf"], 17),
        "meter": face(["seguibl.ttf", "segoeuib.ttf", "DejaVuSans-Bold.ttf"], 30),
        "foot": face(["segoeui.ttf", "DejaVuSans.ttf"], 16),
    }


def compact(n: int) -> str:
    for size, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= size:
            return f"{n / size:.1f}".rstrip("0").rstrip(".") + suffix
    return str(n)


def _wrap(draw, text: str, font, width: int, limit: int) -> list[str]:
    """At most `limit` lines of `text` that fit `width`, the last ending in … if it was cut."""
    lines, line = [], ""
    for word in EMOJI.sub("", " ".join(text.split())).split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=font) <= width or not line:
            line = trial
        else:
            lines.append(line)
            line = word
    lines.append(line)
    lines = [part for part in lines if part]
    cut = len(lines) > limit
    lines = lines[:limit]
    while lines and (cut or draw.textlength(lines[-1], font=font) > width):
        if draw.textlength(lines[-1] + "…", font=font) <= width:
            lines[-1] += "…"
            break
        lines[-1] = lines[-1][:-1].rstrip()
    return lines


def _avatar(post: Post, size: int):
    from PIL import Image, ImageDraw

    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    mask = mask.resize((size, size), Image.LANCZOS)
    try:
        face = Image.open(io.BytesIO(post.avatar)).convert("RGB").resize((size, size), Image.LANCZOS)
    except Exception:
        face = Image.new("RGB", (size, size), LINE)
        initial = (post.name or post.handle or "?")[0].upper()
        draw = ImageDraw.Draw(face)
        font = _get_font(["segoeuib.ttf", "DejaVuSans-Bold.ttf"], size // 2)
        draw.text((size / 2, size / 2), initial, font=font, fill=MUTED, anchor="mm")
    return face, mask


def render(posts: list[Post], period: str, channel: str, shared: int, dealers: list[tuple[str, int]]) -> bytes:
    from PIL import Image, ImageDraw

    f = _fonts()
    height = HEAD + ROW * max(len(posts), 1) + FOOT
    image = Image.new("RGB", (W, height), BG)
    draw = ImageDraw.Draw(image)

    # Header: kicker, title, the period, then chips
    draw.rectangle((0, 0, W, 6), fill=SLIME)
    draw.text((PAD, 34), "S A N T I   P R E S E N T S", font=f["kicker"], fill=SLIME)
    draw.text((PAD, 56), "The Slop Report", font=f["title"], fill=TEXT)
    scope = "all time" if period == "all" else f"the past {period}"
    draw.text((W - PAD, 92), f"#{channel} · {scope}", font=f["sub"], fill=MUTED, anchor="rm")
    chips = [f"{shared} posts shared", f"{len({name for name, _ in dealers})} dealers"]
    if dealers:
        chips.append(f"top dealer: {dealers[0][0]} ×{dealers[0][1]}")
    x = PAD
    for index, chip in enumerate(chips):
        width = draw.textlength(chip, font=f["chip"]) + 28
        accent = index == len(chips) - 1 and dealers
        draw.rounded_rectangle((x, 148, x + width, 182), radius=17, fill=(32, 44, 28) if accent else PANEL,
                               outline=SLIME if accent else LINE)
        draw.text((x + 14, 165), chip, font=f["chip"], fill=SLIME if accent else MUTED, anchor="lm")
        x += width + 10

    if not posts:
        draw.text((W / 2, HEAD + ROW / 2), "No posts shared yet. Suspiciously clean.", font=f["sub"], fill=MUTED, anchor="mm")

    for index, post in enumerate(posts):
        top = HEAD + index * ROW
        draw.rounded_rectangle((PAD - 12, top, W - PAD + 12, top + ROW - 14), radius=18, fill=PANEL)
        draw.text((PAD + 30, top + 54), f"{index + 1}", font=f["rank"], fill=SLIME if index == 0 else DIM, anchor="mm")
        face, mask = _avatar(post, 64)
        image.paste(face, (PAD + 70, top + 22), mask)

        left, right = PAD + 152, W - PAD - 210
        draw.text((left, top + 20), post.name or post.handle, font=f["name"], fill=TEXT)
        name_w = draw.textlength(post.name or post.handle, font=f["name"])
        if post.handle and post.name:
            draw.text((left + name_w + 10, top + 24), f"@{post.handle}", font=f["handle"], fill=MUTED)
        for line_no, line in enumerate(_wrap(draw, post.text or "(no text: just media)", f["body"], right - left, 2)):
            draw.text((left, top + 56 + line_no * 29), line, font=f["body"], fill=TEXT if post.text else MUTED)
        sharers = list(dict.fromkeys(share["sharer"] for share in post.shared))
        bits = [f"{compact(post.likes)} likes", f"{compact(post.reposts)} reposts"]
        if post.views:
            bits.append(f"{compact(post.views)} views")
        bits.append("shared by " + ", ".join(sharers[:2]) + (f" +{len(sharers) - 2}" if len(sharers) > 2 else ""))
        if post.reacts:
            bits.append(f"{post.reacts} react{'s' * (post.reacts != 1)} here")
        draw.text((left, top + 122), "  ·  ".join(bits), font=f["stats"], fill=MUTED)

        # The slop meter
        mx = W - PAD - 180
        color = KIND_COLORS.get(post.kind, MUTED)
        if post.kind:
            label = post.kind.upper()
            lw = draw.textlength(label, font=f["chip"]) + 22
            draw.rounded_rectangle((mx, top + 22, mx + lw, top + 50), radius=14, outline=color, width=2)
            draw.text((mx + 11, top + 36), label, font=f["chip"], fill=color, anchor="lm")
        if post.sloppiness is not None:
            percent = f"{post.sloppiness:.0%}"
            draw.text((mx, top + 98), percent, font=f["meter"], fill=TEXT, anchor="ls")
            draw.text((mx + draw.textlength(percent, font=f["meter"]) + 8, top + 98), "slop", font=f["stats"], fill=MUTED, anchor="ls")
            draw.rounded_rectangle((mx, top + 112, mx + 180, top + 122), radius=5, fill=LINE)
            fill = 180 * max(0.04, min(1.0, post.sloppiness))
            draw.rounded_rectangle((mx, top + 112, mx + fill, top + 122), radius=5, fill=SLIME)

    draw.text((PAD, height - FOOT / 2 - 4), "Ranked by reach and reactions · rated by Jev · no GPU was woken",
              font=f["foot"], fill=DIM, anchor="lm")
    draw.text((W - PAD, height - FOOT / 2 - 4), "Mecha Epstein", font=f["foot"], fill=DIM, anchor="rm")
    out = io.BytesIO()
    image.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ── The whole report ────────────────────────────────────────────────

async def report(bot: PsychographBot, channel: discord.TextChannel, period: str) -> tuple[discord.Embed, discord.File]:
    await collect(bot, channel)
    posts, shares = await rank(bot, channel.id, PERIODS[period])
    top = posts[:SHOWN]
    await rate(bot, top)
    counts: dict[str, int] = {}
    for share in shares:
        counts[share["sharer"]] = counts.get(share["sharer"], 0) + 1
    dealers = sorted(counts.items(), key=lambda item: -item[1])
    png = await asyncio.to_thread(render, top, period, channel.name, len(shares), dealers)
    lines = [
        f"`{index}` [{(post.name or post.handle)[:40]}](<{post.url}>) · [shared here]({post.jump})"
        for index, post in enumerate(top, 1)
    ]
    embed = discord.Embed(description="\n".join(lines) or None, color=0x96E85C)
    embed.set_image(url="attachment://slop-report.png")
    return embed, discord.File(io.BytesIO(png), filename="slop-report.png")
