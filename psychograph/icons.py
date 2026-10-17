"""The bot's own icons, drawn here and uploaded once as application emojis (usable in every server).

Each icon is drawn with Pillow at startup; an icon already uploaded under its name is reused, so a
restart costs one list call. `icon()` falls back to a unicode emoji until the upload has happened
(or when it can't: missing permission, no network).
"""

from __future__ import annotations

import io
import logging
import math
from collections.abc import Callable
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from .bot import PsychographBot

log = logging.getLogger("psychograph.icons")

SIZE = 128
_OUTLINE = (58, 44, 74, 255)
_INK = (255, 255, 255, 255)
_DIE_COLOURS = {  # pastel, one per die, so a glance tells them apart
    4: (255, 179, 186), 6: (255, 223, 150), 8: (186, 255, 201), 10: (186, 225, 255), 12: (214, 190, 255), 20: (255, 196, 240),
}


def _polygon(sides: int, radius: float, rotation: float = -90, centre: tuple[float, float] = (SIZE / 2, SIZE / 2)) -> list[tuple[float, float]]:
    return [
        (centre[0] + radius * math.cos(math.radians(rotation + 360 * index / sides)),
         centre[1] + radius * math.sin(math.radians(rotation + 360 * index / sides)))
        for index in range(sides)
    ]


def _die(sides: int) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    image = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    fill, r = (*_DIE_COLOURS[sides], 255), SIZE * 0.46
    if sides == 4:
        shape = _polygon(3, SIZE * 0.6, centre=(SIZE / 2, SIZE * 0.64))
    elif sides == 6:
        draw.rounded_rectangle((12, 12, SIZE - 12, SIZE - 12), radius=22, fill=fill, outline=_OUTLINE, width=7)
        shape = None
    elif sides == 8:
        shape = _polygon(4, r)
    elif sides == 10:
        top, middle, bottom = (SIZE / 2, 6), SIZE * 0.6, (SIZE / 2, SIZE - 6)
        shape = [top, (SIZE - 10, middle), bottom, (10, middle)]
    elif sides == 12:
        shape = _polygon(5, r)
    else:
        shape = _polygon(6, r, rotation=-90)
    if shape:
        draw.polygon(shape, fill=fill, outline=_OUTLINE, width=7)
    if sides == 20:  # the inner triangle that makes a hexagon read as a d20
        inner = _polygon(3, r * 0.86, rotation=-90, centre=(SIZE / 2, SIZE * 0.55))
        draw.polygon(inner, outline=tuple(int(channel * 0.82) for channel in fill[:3]), width=5)
    font = ImageFont.load_default(size=40 if sides < 10 else 34)
    label = str(sides)
    box = draw.textbbox((0, 0), label, font=font, stroke_width=5)
    centre_y = SIZE * (0.7 if sides == 4 else 0.6 if sides == 20 else 0.5)
    position = (SIZE / 2 - (box[2] - box[0]) / 2 - box[0], centre_y - (box[3] - box[1]) / 2 - box[1])
    draw.text(position, label, font=font, fill=_INK, stroke_width=5, stroke_fill=_OUTLINE)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


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
