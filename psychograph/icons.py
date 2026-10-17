"""The bot's own icons, drawn here and uploaded once as application emojis (usable in every server).

Each icon is drawn with Pillow at startup; an icon already uploaded under its name is reused, so a
restart costs one list call. `icon()` falls back to a unicode emoji until the upload has happened
(or when it can't: missing permission, no network).
"""

from __future__ import annotations

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
FONT = Path(__file__).resolve().parent.parent / "assets" / "fonts" / "Fredoka.ttf"
_OUTLINE = (58, 44, 74, 255)
_INK = (255, 255, 255, 255)
_DIE_COLOURS = {  # pastel, one per die, so a glance tells them apart
    4: (255, 168, 178), 6: (255, 214, 120), 8: (150, 232, 170), 10: (150, 205, 255), 12: (196, 168, 255), 20: (255, 170, 225),
}
_TONES = {"crit": (255, 208, 70), "fumble": (168, 168, 184)}  # nat 20 gold, nat 1 ash


def _polygon(sides: int, radius: float, centre: tuple[float, float], rotation: float = -90) -> list[tuple[float, float]]:
    return [
        (centre[0] + radius * math.cos(math.radians(rotation + 360 * index / sides)),
         centre[1] + radius * math.sin(math.radians(rotation + 360 * index / sides)))
        for index in range(sides)
    ]


def _font(size: int):
    from PIL import ImageFont

    try:
        font = ImageFont.truetype(str(FONT), size)
        font.set_variation_by_name("Bold")
        return font
    except (OSError, ValueError):
        return ImageFont.load_default(size=size)


def draw_die(sides: int, label: str | None = None, size: int = SIZE, tone: str = "normal"):
    """A die face as an RGBA image: its shape and colour by `sides`, with `label` (default: the side count)
    as large as the face allows. tone "crit" is gold with sparkles, "fumble" ash grey with a crack."""
    from PIL import Image, ImageDraw

    sides = sides if sides in _DIE_COLOURS else 20
    big = size * SCALE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    fill = (*_TONES.get(tone, _DIE_COLOURS[sides]), 255)
    line = max(2, round(big * 0.05))
    margin = line / 2 + big * 0.01
    centre, radius = (big / 2, big / 2), big / 2 - margin
    if sides == 4:
        shape = _polygon(3, radius * 1.12, (big / 2, big * 0.6))
    elif sides == 6:
        draw.rounded_rectangle((margin, margin, big - margin, big - margin), radius=big * 0.2, fill=fill, outline=_OUTLINE, width=line)
        shape = None
    elif sides == 8:
        shape = _polygon(4, radius, centre)
    elif sides == 10:
        shape = [(big / 2, margin), (big - margin, big * 0.58), (big / 2, big - margin), (margin, big * 0.58)]
    elif sides == 12:
        shape = _polygon(5, radius, (big / 2, big * 0.53))
    else:
        shape = _polygon(6, radius, centre)
    if shape:
        draw.polygon(shape, fill=fill, outline=_OUTLINE, width=line)
    if sides == 20:  # the inner facet that makes a hexagon read as a d20
        inner = _polygon(3, radius * 0.9, (big / 2, big * 0.55))
        draw.polygon(inner, outline=tuple(int(channel * 0.8) for channel in fill[:3]), width=max(1, line // 2))
    if tone == "fumble":
        crack = [(big * 0.62, big * 0.08), (big * 0.52, big * 0.3), (big * 0.62, big * 0.42), (big * 0.48, big * 0.62)]
        draw.line(crack, fill=_OUTLINE, width=max(1, line // 2), joint="curve")
    label = str(sides) if label is None else label
    # Fit the label to the face: d4's triangle has the least room, low and narrow.
    room_w, room_h, centre_y = {4: (0.5, 0.36, 0.66), 20: (0.6, 0.4, 0.56), 10: (0.56, 0.4, 0.56)}.get(sides, (0.66, 0.5, 0.52))
    stroke = max(1, round(big * 0.035))
    font_size = round(big * room_h)
    while font_size > 8:
        font = _font(font_size)
        box = draw.textbbox((0, 0), label, font=font, stroke_width=stroke)
        if box[2] - box[0] <= big * room_w and box[3] - box[1] <= big * room_h:
            break
        font_size = round(font_size * 0.92)
    position = (big / 2 - (box[0] + box[2]) / 2, big * centre_y - (box[1] + box[3]) / 2)
    draw.text(position, label, font=font, fill=_INK, stroke_width=stroke, stroke_fill=_OUTLINE)
    if tone == "crit":
        for x, y, r in ((0.13, 0.15, 0.1), (0.88, 0.22, 0.075), (0.86, 0.85, 0.085)):
            _sparkle(draw, big * x, big * y, big * r)
    return image.resize((size, size), Image.LANCZOS)


def _sparkle(draw, x: float, y: float, r: float) -> None:
    points = [(x, y - r), (x + r * 0.28, y - r * 0.28), (x + r, y), (x + r * 0.28, y + r * 0.28),
              (x, y + r), (x - r * 0.28, y + r * 0.28), (x - r, y), (x - r * 0.28, y - r * 0.28)]
    draw.polygon(points, fill=(255, 255, 255, 255), outline=_OUTLINE, width=max(1, round(r * 0.18)))


def png(image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _die(sides: int) -> bytes:
    return png(draw_die(sides))


# name → (drawing, fallback emoji)
ICONS: dict[str, tuple[Callable[[], bytes], str]] = {
    f"die_d{sides}": ((lambda sides=sides: _die(sides)), "🎲") for sides in (4, 6, 8, 10, 12, 20)
}


def icon(bot: PsychographBot, name: str) -> str:
    return bot.icons.get(name) or ICONS[name][1]


async def sync(bot: PsychographBot) -> None:
    """Upload any icon that isn't an application emoji yet; remember all of them for `icon()`."""
    try:
        existing = {emoji.name: emoji for emoji in await bot.fetch_application_emojis()}
        for name, (draw, _fallback) in ICONS.items():
            emoji = existing.get(name)
            if emoji is None:
                emoji = await bot.create_application_emoji(name=name, image=draw())
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
