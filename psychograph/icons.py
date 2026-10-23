"""The bot's own icons, drawn here and uploaded once as application emojis (usable in every server).

Each icon is drawn with Pillow at startup; an icon already uploaded under its name is reused, so a
restart costs one list call. `icon()` falls back to a unicode emoji until the upload has happened
(or when it can't: missing permission, no network).
"""

from __future__ import annotations

import hashlib
import io
import logging
import math
from pathlib import Path
from collections.abc import Callable
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .bot import PsychographBot

log = logging.getLogger("psychograph.icons")

SIZE = 128
SCALE = 4  # drawn this many times larger, then scaled down: Pillow's polygons have no anti-aliasing
FONTS = Path(__file__).resolve().parent.parent / "assets" / "fonts"
NUMERAL_FONT = FONTS / "BarlowSemiCondensed-SemiBold.ttf"
# Charcoal and bone, with brass for a natural 20 and red for a natural 1: a solid face reads on light and dark Discord.
CHARCOAL = (20, 22, 27, 255)
BONE = (236, 230, 218, 255)
FACET = (236, 230, 218, 110)
BRASS = (200, 161, 90, 255)
EMBER = (180, 69, 58, 255)
_TONES = {  # tone → (face, edge, facet lines, numeral)
    "normal": (CHARCOAL, BONE, FACET, BONE),
    "crit": (BRASS, CHARCOAL, (20, 22, 27, 120), CHARCOAL),
    "fumble": (CHARCOAL, EMBER, (180, 69, 58, 130), EMBER),
}


def _polygon(sides: int, radius: float, centre: tuple[float, float], rotation: float = -90) -> list[tuple[float, float]]:
    return [
        (centre[0] + radius * math.cos(math.radians(rotation + 360 * index / sides)),
         centre[1] + radius * math.sin(math.radians(rotation + 360 * index / sides)))
        for index in range(sides)
    ]


def _font(size: int):
    from PIL import ImageFont

    try:
        return ImageFont.truetype(str(NUMERAL_FONT), size)
    except OSError:
        return ImageFont.load_default(size=size)


def _shape(sides: int, big: float, margin: float):
    """(outline, facet lines, centre of the face the numeral sits on, room for it as (width, height) fractions)."""
    c = big / 2
    r = big / 2 - margin
    if sides == 4:
        outline = _polygon(3, r * 1.1, (c, big * 0.6))
        middle = (c, big * 0.6)
        return outline, [(middle, point) for point in outline], (c, big * 0.66), (0.34, 0.28)
    if sides == 6:
        k = margin
        return [(k, k), (big - k, k), (big - k, big - k), (k, big - k)], [], (c, c), (0.62, 0.5)
    if sides == 8:
        outline = _polygon(4, r, (c, c))
        return outline, [(outline[1], outline[3])], (c, big * 0.58), (0.42, 0.3)
    if sides == 10:
        top, bottom = (c, margin), (c, big - margin)
        left, right = (margin, big * 0.56), (big - margin, big * 0.56)
        inner = (c, big * 0.72)
        return [top, right, bottom, left], [(top, inner), (left, inner), (right, inner), (inner, bottom)], (c, big * 0.5), (0.36, 0.26)
    if sides == 12:
        outline = _polygon(5, r, (c, big * 0.53))
        inner = _polygon(5, r * 0.52, (c, big * 0.53))
        return outline, [*zip(inner, inner[1:] + inner[:1]), *zip(inner, outline)], (c, big * 0.53), (0.5, 0.36)
    outline = _polygon(6, r, (c, c))  # d20: a hexagon around a front triangle, joined vertex to vertex
    inner = _polygon(3, r * 0.74, (c, big * 0.52))
    lines = [*zip(inner, inner[1:] + inner[:1])]
    lines += [(inner[0], outline[0]), (inner[1], outline[2]), (inner[2], outline[4])]
    lines += [(inner[0], outline[1]), (inner[0], outline[5]), (inner[1], outline[1]), (inner[1], outline[3]),
              (inner[2], outline[3]), (inner[2], outline[5])]
    return outline, lines, (c, big * 0.6), (0.4, 0.3)


def draw_die(sides: int, label: str | None = None, size: int = SIZE, tone: str = "normal"):
    """A die face as an RGBA image: a solid polyhedron in line art (its real facet lines), with `label` (default: the
    side count) set as large as the front face allows. tone "crit" is brass, "fumble" ember-edged and cracked."""
    from PIL import Image, ImageChops, ImageDraw

    sides = sides if sides in (4, 6, 8, 10, 12, 20) else 20
    face, edge, facet, numeral = _TONES.get(tone, _TONES["normal"])
    big = size * SCALE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    line = max(2, round(big * 0.028))
    margin = line + big * 0.02
    outline, facets, centre, (room_w, room_h) = _shape(sides, big, margin)
    draw.polygon(outline, fill=face)
    for a, b in facets:
        draw.line([a, b], fill=facet, width=max(1, round(line * 0.55)))
    label = str(sides) if label is None else label
    if len(label) >= 3:
        room_w *= 1.25
    font_size = round(big * room_h * 1.35)
    while font_size > 8:
        font = _font(font_size)
        box = draw.textbbox((0, 0), label, font=font)
        if box[2] - box[0] <= big * room_w and box[3] - box[1] <= big * room_h:
            break
        font_size = round(font_size * 0.93)
    # Knock the facet lines out behind the numeral so it stays crisp at emoji size.
    pad = big * 0.025
    knock = (centre[0] - (box[2] - box[0]) / 2 - pad, centre[1] - (box[3] - box[1]) / 2 - pad,
             centre[0] + (box[2] - box[0]) / 2 + pad, centre[1] + (box[3] - box[1]) / 2 + pad)
    if facets:
        draw.rectangle(knock, fill=face)
    draw.polygon(outline, outline=edge, width=line)
    draw.text((centre[0] - (box[0] + box[2]) / 2, centre[1] - (box[1] + box[3]) / 2), label, font=font, fill=numeral)
    if tone == "fumble":  # split clean through: a gap you can see the table through, with a branch and a chip
        die_alpha = image.getchannel("A")  # the crack stays inside the die's silhouette
        _fissure(draw, [(0.70, -0.02), (0.63, 0.16), (0.72, 0.27), (0.62, 0.40), (0.69, 0.50), (0.64, 0.58)], big, big * 0.085)
        _fissure(draw, [(0.63, 0.16), (0.52, 0.22), (0.45, 0.18)], big, big * 0.04)
        _fissure(draw, [(0.04, 0.70), (0.17, 0.76), (0.20, 0.86)], big, big * 0.05)
        image.putalpha(ImageChops.darker(image.getchannel("A"), die_alpha))
    return image.resize((size, size), Image.LANCZOS)


def _fissure(draw, path: list[tuple[float, float]], big: float, width: float) -> None:
    """A crack as a tapering wedge cut out of the die (wide where it breaks the edge, closing to nothing), rimmed in ember."""
    points = [(x * big, y * big) for x, y in path]
    left, right = [], []
    for index, (x, y) in enumerate(points):
        nx, ny = points[min(index + 1, len(points) - 1)]
        px, py = points[max(index - 1, 0)]
        dx, dy = nx - px, ny - py
        length = math.hypot(dx, dy) or 1
        half = width * (1 - index / (len(points) - 1)) ** 0.8 / 2
        left.append((x - dy / length * half, y + dx / length * half))
        right.append((x + dy / length * half, y - dx / length * half))
    wedge = left + right[::-1]
    draw.polygon(wedge, fill=(0, 0, 0, 0))
    draw.line(left, fill=EMBER, width=max(1, round(width * 0.14)), joint="curve")
    draw.line(right, fill=EMBER, width=max(1, round(width * 0.14)), joint="curve")


def png(image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _die(sides: int) -> bytes:
    return png(draw_die(sides))


def draw_noumenon(size: int = SIZE):
    """The Outside's eye (#nanospasm, outside.py): an almond of bone on a transparent ground, a brass iris, a slit."""
    from PIL import Image, ImageDraw

    big = size * SCALE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    half, rise = big * 0.46, big * 0.27
    lid = [(big / 2 + half * math.cos(math.radians(t)), big / 2 - rise * abs(math.sin(math.radians(t))) ** 0.9 * (1 if t <= 180 else -1))
           for t in range(0, 361, 4)]  # an ellipse pinched at the corners: an almond
    draw.polygon(lid, fill=CHARCOAL, outline=BONE, width=round(big * 0.045))
    iris = big * 0.2
    draw.ellipse((big / 2 - iris, big / 2 - iris, big / 2 + iris, big / 2 + iris), fill=BRASS)
    slit = big * 0.045
    draw.ellipse((big / 2 - slit, big / 2 - iris * 0.92, big / 2 + slit, big / 2 + iris * 0.92), fill=CHARCOAL)
    return image.resize((size, size), Image.LANCZOS)


# name → (drawing, fallback emoji)
ICONS: dict[str, tuple[Callable[[], bytes], str]] = {
    f"die_d{sides}": ((lambda sides=sides: _die(sides)), "🎲") for sides in (4, 6, 8, 10, 12, 20)
}
ICONS["noumenon"] = ((lambda: png(draw_noumenon())), "👁️")


def icon(bot: PsychographBot, name: str) -> str:
    return bot.icons.get(name) or ICONS[name][1]


async def sync(bot: PsychographBot) -> None:
    """Upload any icon that isn't an application emoji yet, or whose drawing has changed since it was uploaded
    (a fingerprint per icon is kept in the store); remember all of them for `icon()`."""
    try:
        existing = {emoji.name: emoji for emoji in await bot.fetch_application_emojis()}
        for name, (draw, _fallback) in ICONS.items():
            art = draw()
            fingerprint = hashlib.sha256(art).hexdigest()[:16]
            emoji = existing.get(name)
            if emoji is not None and bot.store.state(f"icon:{name}") != fingerprint:
                await emoji.delete()
                emoji = None
            if emoji is None:
                emoji = await bot.create_application_emoji(name=name, image=art)
                bot.store.set_state(f"icon:{name}", fingerprint)
                log.info("Uploaded icon %s", name)
            bot.icons[name] = str(emoji)
    except (discord.HTTPException, OSError) as error:
        log.info("Icons fall back to unicode: %s", error)


def roll_image(dice: list[tuple[int, str, str]], size: int = 160):
    """A strip of result dice, (sides, label, tone) each, wrapping after six; sized down as sets grow."""
    from PIL import Image

    size = size if len(dice) == 1 else 112
    per_row = min(len(dice), 6)
    rows = (len(dice) + 5) // 6
    gap = 8
    strip = Image.new("RGBA", (per_row * (size + gap) - gap, rows * (size + gap) - gap), (0, 0, 0, 0))
    for index, (sides, label, tone) in enumerate(dice):
        strip.alpha_composite(draw_die(sides, label, size, tone), ((index % 6) * (size + gap), (index // 6) * (size + gap)))
    return strip
