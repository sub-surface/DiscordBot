"""Character sheets drawn as images: charcoal, bone and a thin brass rule (see icons.py for the palette).

Drawn at twice the size and scaled down, so the hairlines and small caps stay crisp in Discord's preview.
"""

from __future__ import annotations

import io

from .characters import STAT_NAMES, STATS, Character, MAX_CHUCKLES, signed
from .icons import BONE, BRASS, CHARCOAL, EMBER, FONTS

W, H = 1200, 700
S = 2  # supersampling
MUTED = (150, 146, 140, 255)
# Solid mixes over charcoal (Pillow replaces alpha rather than blending it on an opaque canvas).
RULE = (126, 104, 63, 255)
HAIR = (58, 59, 62, 255)


def _font(name: str, size: int):
    from PIL import ImageFont

    return ImageFont.truetype(str(FONTS / name), size * S)


def _spaced(draw, xy, text: str, font, fill, tracking: float = 0.12, anchor_right: bool = False) -> float:
    """Letter-spaced small caps (Pillow has no tracking). Returns the width drawn."""
    x, y = xy
    spacing = font.size * tracking
    widths = [draw.textlength(char, font=font) for char in text]
    total = sum(widths) + spacing * max(0, len(text) - 1)
    if anchor_right:
        x -= total
    for char, width in zip(text, widths):
        draw.text((x, y), char, font=font, fill=fill)
        x += width + spacing
    return total


def _wrap(draw, text: str, font, width: float) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=font) <= width:
            line = candidate
        else:
            lines.append(line)
            line = word
    return [*lines, line] if line else lines


def _portrait(image_bytes: bytes | None, side: int, initials: str):
    from PIL import Image, ImageDraw, ImageOps

    if image_bytes:
        try:
            with Image.open(io.BytesIO(image_bytes)) as source:
                art = ImageOps.fit(source.convert("RGBA"), (side, side), Image.LANCZOS)
            tile = Image.new("RGBA", (side, side), (32, 34, 41, 255))  # transparent avatars sit on the dark tile
            tile.alpha_composite(art)
            return tile.convert("RGB")
        except Exception:  # a broken avatar falls back to initials
            pass
    tile = Image.new("RGB", (side, side), (32, 34, 41))
    draw = ImageDraw.Draw(tile)
    font = _font("Marcellus-Regular.ttf", side // (2 * S))
    draw.text((side / 2, side / 2), initials, font=font, fill=BONE, anchor="mm")
    return tile


def render(character: Character, player: str, portrait: bytes | None = None):
    from PIL import Image, ImageDraw

    image = Image.new("RGBA", (W * S, H * S), CHARCOAL)
    draw = ImageDraw.Draw(image)
    inset = 22 * S
    draw.rectangle((inset, inset, W * S - inset, H * S - inset), outline=RULE, width=S)

    # Portrait, framed in a hairline with a brass corner tick.
    side = 236 * S
    px, py = 64 * S, 64 * S
    words = [word for word in character.name.split() if word[:1].isupper()] or character.name.split()
    initials = "".join(word[0] for word in (words[:1] + words[-1:] if len(words) > 1 else words)).upper() or "?"
    image.paste(_portrait(portrait, side, initials), (px, py))
    draw.rectangle((px, py, px + side, py + side), outline=BONE, width=S)
    draw.line((px - 8 * S, py + side + 8 * S, px + 40 * S, py + side + 8 * S), fill=BRASS, width=2 * S)

    # Name and standing.
    nx = px + side + 48 * S
    name_font = _font("Marcellus-Regular.ttf", 62)
    name = character.name
    while draw.textlength(name, font=name_font) > (W - 64) * S - nx and name_font.size > 30 * S:
        name_font = _font("Marcellus-Regular.ttf", name_font.size // S - 4)
    draw.text((nx, 58 * S), name, font=name_font, fill=BONE)
    role, faction = character.role_info, character.faction_info
    standing = f"LEVEL {character.level} {role.name.upper()} ({role.classic.upper()})  ·  {faction.name.upper()}"
    _spaced(draw, (nx, 140 * S), standing, _font("BarlowSemiCondensed-SemiBold.ttf", 20), BRASS, 0.14)
    sub = f"{faction.nickname} · played by {player}" if faction.key != "none" else f"played by {player}"
    draw.text((nx, 170 * S), sub, font=_font("BarlowSemiCondensed-Italic.ttf", 22), fill=MUTED)

    # The four scores: label, score, modifier, divided by hairlines.
    top, col_w = 218 * S, ((W - 64) * S - nx) / 4
    label_font, score_font, mod_font = (_font("BarlowSemiCondensed-SemiBold.ttf", 19), _font("BarlowSemiCondensed-Medium.ttf", 76),
                                        _font("BarlowSemiCondensed-SemiBold.ttf", 24))
    for index, stat in enumerate(STATS):
        x = nx + index * col_w
        if index:
            draw.line((x - 1, top + 6 * S, x - 1, top + 116 * S), fill=HAIR, width=S)
        cx = x + col_w / 2
        label = STAT_NAMES[stat]
        tracked = sum(draw.textlength(char, font=label_font) for char in label) + label_font.size * 0.12 * (len(label) - 1)
        _spaced(draw, (cx - tracked / 2, top), label, label_font, BRASS if stat == role.stat else MUTED, 0.12)
        draw.text((cx, top + 66 * S), str(character.scores.get(stat, 10)), font=score_font, fill=BONE, anchor="mm")
        draw.text((cx, top + 112 * S), signed(character.mod(stat)), font=mod_font, fill=MUTED, anchor="mm")

    # Vitals row, under the portrait: hit points as ticks, keks, chuckles.
    vy = 336 * S
    small = _font("BarlowSemiCondensed-SemiBold.ttf", 18)
    value = _font("BarlowSemiCondensed-Medium.ttf", 30)
    _spaced(draw, (px, vy), "HIT POINTS", small, MUTED)
    draw.text((px + side, vy - 6 * S), f"{character.hp}/{character.max_hp}", font=value, fill=BONE, anchor="ra")
    ticks = character.max_hp
    gap = 4 * S
    tick_w = (side - gap * (ticks - 1)) / max(ticks, 1)
    for index in range(ticks):
        x = px + index * (tick_w + gap)
        filled = index < character.hp
        draw.rectangle((x, vy + 34 * S, x + tick_w, vy + 44 * S), fill=BONE if filled else None, outline=BONE if filled else HAIR, width=S)
    _spaced(draw, (px, vy + 70 * S), "KEKS", small, MUTED)
    draw.text((px + side, vy + 64 * S), f"{character.keks:,}", font=value, fill=BONE, anchor="ra")
    _spaced(draw, (px, vy + 116 * S), "CHUCKLES", small, MUTED)
    for index in range(MAX_CHUCKLES):  # little diamonds, brass when held
        cx, cy, r = px + side - 12 * S - index * 30 * S, vy + 124 * S, 9 * S
        points = [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)]
        held = MAX_CHUCKLES - index <= character.chuckles
        draw.polygon(points, fill=BRASS if held else None, outline=BRASS if held else HAIR, width=S)

    # The pack: what they carry (from the ledger), quest items first, under the vitals.
    if character.inventory:
        y = vy + 168 * S
        draw.line((px, y - 14 * S, px + side, y - 14 * S), fill=HAIR, width=S)
        _spaced(draw, (px, y), "PACK", small, MUTED)
        item_font, qty_font = _font("BarlowSemiCondensed-Medium.ttf", 22), _font("BarlowSemiCondensed-SemiBold.ttf", 20)
        items = sorted(character.inventory, key=lambda item: item.get("kind") != "quest")
        shown = items[:5] if len(items) <= 5 else items[:4]
        y += 30 * S
        for item in shown:
            qty = f"×{item['qty']}" if item["qty"] != 1 else ""
            room = side - (draw.textlength(qty, font=qty_font) + 10 * S if qty else 0)
            name = item["name"]
            while draw.textlength(name, font=item_font) > room and len(name) > 4:
                name = name[:-2].rstrip() + "…"
            draw.text((px, y), name, font=item_font, fill=BRASS if item.get("kind") == "quest" else BONE)
            if qty:
                draw.text((px + side, y + 2 * S), qty, font=qty_font, fill=MUTED, anchor="ra")
            y += 29 * S
        if len(items) > len(shown):
            draw.text((px, y), f"and {len(items) - len(shown)} more", font=_font("BarlowSemiCondensed-Italic.ttf", 20), fill=MUTED)

    # The class move and the keepsake, under the scores.
    text_w = (W - 64) * S - nx
    my = 374 * S
    draw.line((nx, my - 18 * S, nx + text_w, my - 18 * S), fill=HAIR, width=S)
    _spaced(draw, (nx, my), role.move.upper(), _font("BarlowSemiCondensed-SemiBold.ttf", 20), BRASS, 0.12)
    body = _font("BarlowSemiCondensed-Medium.ttf", 25)
    y = my + 34 * S
    for line in _wrap(draw, role.move_text, body, text_w)[:3]:
        draw.text((nx, y), line, font=body, fill=BONE)
        y += 33 * S
    if character.keepsake:
        y += 16 * S
        _spaced(draw, (nx, y), "KEEPSAKE", _font("BarlowSemiCondensed-SemiBold.ttf", 20), MUTED, 0.12)
        italic = _font("BarlowSemiCondensed-Italic.ttf", 25)
        y += 32 * S
        for line in _wrap(draw, character.keepsake[:1].upper() + character.keepsake[1:], italic, text_w)[:2]:
            draw.text((nx, y), line, font=italic, fill=BONE)
            y += 33 * S
    if character.look:
        y += 16 * S
        _spaced(draw, (nx, y), "LOOK", _font("BarlowSemiCondensed-SemiBold.ttf", 20), MUTED, 0.12)
        italic = _font("BarlowSemiCondensed-Italic.ttf", 25)
        y += 32 * S
        for line in _wrap(draw, character.look, italic, text_w)[:2]:
            draw.text((nx, y), line, font=italic, fill=BONE)
            y += 33 * S
    if character.hp <= 0:
        _spaced(draw, (W * S - 70 * S, 58 * S), "DOWN", _font("BarlowSemiCondensed-Bold.ttf", 22), EMBER, 0.2, anchor_right=True)
    return image.resize((W, H), Image.LANCZOS)


def png(character: Character, player: str, portrait: bytes | None = None) -> bytes:
    buffer = io.BytesIO()
    render(character, player, portrait).save(buffer, format="PNG")
    return buffer.getvalue()
