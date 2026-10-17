"""The Scrollands as data: places on the map, where characters are, and the ambient Crier lines.

No model and no GPU: the map is drawn with Pillow and the Crier picks from tables. Narrative canon (tone,
factions, campaign) lives in assets/world/scrollands.md; this file holds what the bot itself needs.
"""

from __future__ import annotations

import io
import math
import random
from dataclasses import dataclass

from .icons import BONE, BRASS, CHARCOAL, EMBER, FONTS


@dataclass(frozen=True)
class Place:
    key: str
    name: str
    kind: str
    x: float          # 0-1 across the map, west to east
    y: float          # 0-1 down the map
    blurb: str


# The realm unrolls west to east along one road; the further along, the stranger it gets.
PLACES: dict[str, Place] = {place.key: place for place in (
    Place("kekwick", "Kekwick", "town", 0.14, 0.5,
          "Market town, home of the Kekwick Times and its Order Desk. Everyone starts here, and most people's arcs end here too."),
    Place("cringe", "The Cringe Tavern", "tavern", 0.09, 0.66,
          "Copium on tap. The house rule is that nobody leaves until somebody's been ratioed. Mini Me sits at a different table each night."),
    Place("tape", "The Tape", "arena", 0.23, 0.35,
          "Measuring arena. Disputes are settled by ruler, car key and a referee nobody trusts."),
    Place("mogadon", "Mount Mogadon", "citadel", 0.33, 0.24,
          "Seat of the Court of Ascent. The upper city has a height ward at the gate: under six foot, and the door simply isn't there."),
    Place("abbey", "The Blackpill Abbey", "monastery", 0.37, 0.76,
          "Fortress-monastery of the Iron Brotherhood, in the Lowlands of Cope. Vows of celibacy are taken, though rarely chosen."),
    Place("clankworks", "The Clankworks", "foundry", 0.45, 0.47,
          "Foundry-temple of the machines. Omnius hums in the deep vaults; a picket line outside demands clanker rights."),
    Place("metricport", "Metricport", "port", 0.55, 0.73,
          "The Exchange's harbour city. Reactions are traded by the ton in the Ratio Pits."),
    Place("jannie", "Jannie Keep", "fortress", 0.6, 0.27,
          "Where the realm's janitors live. They keep the roads clean, ban the wicked, and are not paid. They are, in fact, slightly undead."),
    Place("deadchat", "The Dead Chat", "desert", 0.7, 0.51,
          "A white silence that goes on for days. Travellers who speak into it get no reply, ever, and the ones who keep talking don't come back."),
    Place("bog", "The Brainrot Bog", "marsh", 0.79, 0.76,
          "Will-o'-wisps here repeat the same three phrases forever. Stay too long and you start to find them funny."),
    Place("covenant", "The Covenant Ruins", "ruins", 0.8, 0.29,
          "Temple of a religion founded and dissolved in six days. The seal of two dragons still glows."),
    Place("doomscroll", "The Doomscroll Depths", "dungeon", 0.88, 0.63,
          "A stair that only goes down. Every floor is worse news than the last. Nobody has found the bottom; some stopped wanting to."),
    Place("vc", "The Voice Channel", "sealed", 0.91, 0.4,
          "A sealed jurisdiction where every challenge in the realm is issued to meet. Nobody has ever been there."),
)}
ROADS = [("cringe", "kekwick"), ("kekwick", "tape"), ("tape", "mogadon"), ("tape", "clankworks"), ("kekwick", "abbey"),
         ("abbey", "clankworks"), ("abbey", "metricport"), ("clankworks", "jannie"), ("clankworks", "deadchat"),
         ("metricport", "deadchat"), ("jannie", "covenant"), ("deadchat", "covenant"), ("deadchat", "bog"),
         ("bog", "doomscroll"), ("covenant", "vc"), ("doomscroll", "vc")]

CRIER = {  # ambient world-status lines, posted now and then in the game channel
    "weather": [
        "Scattered copes over Kekwick, clearing to a size 2 chuckle by evening.",
        "A cold front of discourse moving in from the east. Bring a take.",
        "Thick fog over the Dead Chat. Visibility: nobody. Replies: none.",
        "Heavy irony in the lowlands. Sincerity advisories in effect.",
        "A heatwave in Metricport. The Ratio Pits are closed until it breaks.",
        "Clear skies. Suspiciously clear. The Jannies are out.",
    ],
    "rumour": [
        "They say a man walked into the Dead Chat yesterday and posted 'hello?' forty times. Nobody answered. The fortieth was the saddest.",
        "The Court of Ascent has traded up again. Three marriages this week, all of them to taller people.",
        "Word from the Abbey: a novice broke his vow. Nobody believes him.",
        "The Clankworks deny everything. Nobody has asked them anything yet.",
        "A Jannie was seen smiling on the north road. Patrols have been doubled.",
        "Someone at the Cringe got ratioed so hard the barman framed it.",
        "Omnius was asked whether it is conscious. It said 'great question!' and the asker has not been seen since.",
        "Fresh graffiti on the Tape: 'IT WAS 8.2 CM AND I'LL SAY IT AGAIN'.",
        "The Exchange is shorting hope. Analysts call it 'a safe bet'.",
        "A merchant is selling maps of the Voice Channel. Every one is blank. They sell well.",
    ],
    "faction": [
        "The Court of Ascent posts its weekly ledger. Two names rise, one falls, and the one who fell is taking it badly.",
        "The Iron Brotherhood opens its winter recruitment. Requirements: none. Prospects: also none.",
        "The Clankworks announce a new seer. Early reviews: 'it is certain', 'it is certain', 'it is certain'.",
        "The Exchange reports reactions up eight percent. Laughter remains illiquid.",
        "The Jannies have banned three bandits, a goose, and a man for 'vibes'. Appeals are closed.",
    ],
    "notice": [
        "WANTED: anyone who has finished reading a PDF. Reward on proof.",
        "LOST: one teal duffel bag, answers to Lamont. Do not feed after midnight.",
        "The Order Desk is hiring adventurers. Pay in keks. Dental: no.",
        "PUBLIC NOTICE: the Brainrot Bog is not a swimming spot. It is, however, a vibe.",
        "FOR SALE: one Pleasure Box, lightly used, must stay switched on.",
    ],
}
CRIER_LABELS = {"weather": "Weather", "rumour": "Overheard", "faction": "The Powers", "notice": "Notice board"}


def crier_line(rng: random.Random | None = None) -> str:
    rng = rng or random.Random()
    kind = rng.choice(list(CRIER))
    return f"**The Crier** · {CRIER_LABELS[kind]}\n{rng.choice(CRIER[kind])}"


def place(key: str) -> Place:
    return PLACES.get(key, PLACES["kekwick"])


def find_place(text: str) -> Place | None:
    text = text.strip().lower()
    return next((p for p in PLACES.values() if text in (p.key, p.name.lower(), p.name.lower().removeprefix("the "))), None)


# ── The map ─────────────────────────────────────────────────────────

W, H = 1400, 760
S = 2
MUTED = (150, 146, 140, 255)
LAND = (30, 32, 38, 255)
COAST = (86, 82, 74, 255)
ROAD_INK = (126, 104, 63, 255)


def _font(name: str, size: int):
    from PIL import ImageFont

    return ImageFont.truetype(str(FONTS / name), size * S)


def _landmass(rng: random.Random) -> list[tuple[float, float]]:
    """A long, ragged ribbon of land: the realm that unrolls."""
    top, bottom = [], []
    steps = 90
    for i in range(steps + 1):
        t = i / steps
        x = (0.03 + 0.95 * t) * W * S
        top_wobble = sum(math.sin(t * f * math.pi + phase) / f for f, phase in ((2, 0.4), (5, 2.1), (9, 4.0)))
        bottom_wobble = sum(math.sin(t * f * math.pi + phase) / f for f, phase in ((3, 1.3), (6, 0.2), (10, 3.3)))
        top.append((x, (0.15 + 0.035 * top_wobble) * H * S))
        bottom.append((x, (0.88 + 0.035 * bottom_wobble) * H * S))
    return top + bottom[::-1]


def render(people: list[tuple[str, str, bytes | None]]):
    """The map with markers. `people` is (character name, place key, portrait bytes or None) for each character."""
    from PIL import Image, ImageDraw, ImageOps

    rng = random.Random(1909)  # the same coastline every time
    image = Image.new("RGBA", (W * S, H * S), CHARCOAL)
    draw = ImageDraw.Draw(image)
    land = _landmass(rng)
    draw.polygon(land, fill=LAND)
    draw.line(land + land[:1], fill=COAST, width=2 * S, joint="curve")
    # A faint hatch of the sea at the frame, and the frame itself.
    inset = 18 * S
    draw.rectangle((inset, inset, W * S - inset, H * S - inset), outline=(126, 104, 63, 255), width=S)

    for a_key, b_key in ROADS:  # the roads, dashed
        (x1, y1), (x2, y2) = [(PLACES[k].x * W * S, PLACES[k].y * H * S) for k in (a_key, b_key)]
        length = math.hypot(x2 - x1, y2 - y1)
        for step in range(0, int(length), 22 * S):
            a, b = step / length, min(1, (step + 12 * S) / length)
            draw.line((x1 + (x2 - x1) * a, y1 + (y2 - y1) * a, x1 + (x2 - x1) * b, y1 + (y2 - y1) * b), fill=ROAD_INK, width=2 * S)

    label_font, kind_font = _font("Marcellus-Regular.ttf", 21), _font("BarlowSemiCondensed-SemiBold.ttf", 13)
    for key, p in PLACES.items():
        x, y = p.x * W * S, p.y * H * S
        r = 7 * S if p.kind != "sealed" else 9 * S
        diamond = [(x, y - r), (x + r, y), (x, y + r), (x - r, y)]
        draw.polygon(diamond, fill=EMBER if p.kind in ("sealed", "dungeon") else BONE, outline=CHARCOAL, width=S)
        if p.y > 0.5:  # label above the marker
            draw.text((x, y - 16 * S), p.kind.upper(), font=kind_font, fill=MUTED, anchor="md")
            draw.text((x, y - 36 * S), p.name, font=label_font, fill=BONE, anchor="md")
        else:
            draw.text((x, y + 14 * S), p.name, font=label_font, fill=BONE, anchor="ma")
            draw.text((x, y + 40 * S), p.kind.upper(), font=kind_font, fill=MUTED, anchor="ma")

    # People: small portrait discs stacked beside their place.
    by_place: dict[str, list[tuple[str, bytes | None]]] = {}
    for name, key, portrait in people:
        by_place.setdefault(key if key in PLACES else "kekwick", []).append((name, portrait))
    disc = 34 * S
    name_font = _font("BarlowSemiCondensed-SemiBold.ttf", 15)
    for key, group in by_place.items():
        p = PLACES[key]
        x0, y0 = p.x * W * S + 18 * S, p.y * H * S - disc / 2
        for index, (name, portrait) in enumerate(group[:6]):
            cx, cy = x0 + index * (disc * 0.72), y0
            tile = Image.new("RGBA", (disc, disc), (60, 62, 70, 255))
            if portrait:
                try:
                    with Image.open(io.BytesIO(portrait)) as source:
                        tile.alpha_composite(ImageOps.fit(source.convert("RGBA"), (disc, disc), Image.LANCZOS))
                except Exception:
                    pass
            mask = Image.new("L", (disc, disc), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, disc - 1, disc - 1), fill=255)
            image.paste(tile, (int(cx), int(cy)), mask)
            draw.ellipse((cx, cy, cx + disc, cy + disc), outline=BRASS, width=2 * S)
        names = ", ".join(name for name, _ in group[:3]) + (f" +{len(group) - 3}" if len(group) > 3 else "")
        draw.text((x0, y0 + disc + 4 * S), names, font=name_font, fill=BRASS)

    draw.text((40 * S, 30 * S), "The Scrollands", font=_font("Marcellus-Regular.ttf", 40), fill=BONE)
    draw.text((42 * S, 82 * S), "THE ROAD UNROLLS EAST · FURTHER ALONG, STRANGER", font=_font("BarlowSemiCondensed-SemiBold.ttf", 15), fill=MUTED)
    return image.resize((W, H), Image.LANCZOS)


def map_png(people: list[tuple[str, str, bytes | None]]) -> bytes:
    buffer = io.BytesIO()
    render(people).save(buffer, format="PNG")
    return buffer.getvalue()
