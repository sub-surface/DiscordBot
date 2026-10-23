"""ASCII avatars for the 45 demons of Pandemonium, drawn procedurally: the same demon always looks the same.

Every avatar is mirrored down the middle (22 columns, two halves of 11), so it is built from left halves: each row is
written up to the centre line and reflected, with / and \\, ( and ), < and > and the rest swapped as it turns.
What a demon looks like comes from what it is:

  the high zone of its net-span     its eyes (ten styles, one per zone)
  the low zone                      its mouth (ten styles)
  its kind                          its frame: Chronodemons are robed and haloed, clockwork; Amphidemons winged and
                                    horned, openings to the Outside; Xenodemons trail tentacles and open a third eye
  syzygetic                         the syzygy's axis, marked above and below
  its name                          the seed for everything else: head shape, horns, ornament, the stars around it

Xenodemons alone break symmetry a little: a few glitch marks drift on one side, the outer gulfs leaking in.
Pure ASCII throughout, so it lines up in any monospace font, Discord's code blocks included.
"""

from __future__ import annotations

import random
import zlib

from .outside import Demon

HALF = 11
_MIRROR = str.maketrans("/\\()<>[]{}dbpq", "\\/)(><][}{bdqp")

# Eyes by the high zone (the left eye; the right is its reflection). A tuple adds a brow row above.
EYES = {
    0: ("o", None), 1: ("'", "_"), 2: ("@", None), 3: ("*", "\\"), 4: ("0", None),
    5: ("^", None), 6: ("d", "~"), 7: ("O", "\\"), 8: ("q", "."), 9: ("x", "/"),
}
# Mouths by the low zone: the end of the left half, nearest the centre.
MOUTHS = {
    0: "--", 1: "\\", 2: "w", 3: "\\_", 4: "(", 5: "V ", 6: "\\VV", 7: "/\\/", 8: "=", 9: "\\._",
}
HEADS = {  # (top-left corner + edge fill, side, bottom-left + fill)
    "round": ((".-", "'"), "(", ("'-.", "_")),
    "moon": ((".-", "~"), "(", ("`._", "_")),
    "box": (("+", "-"), "|", ("+", "-")),
    "skull": ((".-", "\""), "|", ("\\", "_")),
}


def _mirror(half: str) -> str:
    half = half.rjust(HALF)[-HALF:]
    return half + half[::-1].translate(_MIRROR)


def _put(half: str, column: int, text: str) -> str:
    """`text` written into a left half, `column` counted from the outer edge (0) to the centre (10)."""
    row = list(half.rjust(HALF))
    for offset, char in enumerate(text):
        if 0 <= column + offset < HALF:
            row[column + offset] = char
    return "".join(row)


def avatar(demon: Demon) -> str:
    rng = random.Random(zlib.crc32(demon.name.encode()))
    high, low = demon.zones
    xeno, amphi = demon.kind.endswith("Xenodemon"), demon.kind.endswith("Amphidemon")
    head = rng.choice(list(HEADS) if not xeno else ["round", "skull", "moon"])
    width = rng.choice((5, 6, 6, 7))                    # head half-width, from the centre out
    edge = HALF - width - 1                              # the column of the head's side
    (top_corner, top_fill), side, (bottom_corner, bottom_fill) = HEADS[head]
    eye, brow = EYES[high]
    eye_at = edge + 1 + max(1, (width - 1) // 2 - 1)
    rows: list[str] = []

    # Above the head: horns, a halo, ears or eyestalks.
    crown = rng.choice(("horns", "ears", "spikes") if amphi else ("halo", "dial", "spikes") if not xeno else ("stalks", "halo", "horns"))
    if crown == "horns":
        tip = max(0, edge - 2)
        rows += [_put("", tip, "\\" if rng.random() < 0.5 else ")"), _put("", tip + 1, "\\\\"), _put("", tip + 2, "\\")]
    elif crown == "ears":
        rows += [_put("", edge, "/\\"), _put("", edge, "/  \\")]
    elif crown == "spikes":
        rows += [_put("", edge + 1, "/\\/\\/\\/\\/\\"[:width * 2 - 2])]
    elif crown == "halo":
        rows += [_put("", HALF - 4, ".-~~"), _put("", HALF - 5, "(    ")]
    elif crown == "dial":
        rows += [_put("", HALF - 3, ".-'"), _put("", HALF - 4, "/ \\ "), _put("", HALF - 4, "\\__\\")]
    elif crown == "stalks":
        bulb = rng.choice("o@*")
        rows += [_put("", edge + 1, bulb), _put("", edge + 1, "\\"), _put("", edge + 2, "\\")]

    # The head: top edge, a brow, the eyes, cheeks, the mouth, the jaw.
    rows.append(_put("", edge, top_corner + top_fill * (width + 1 - len(top_corner))))
    if xeno:  # the third eye, on the forehead
        rows.append(_put(_put("", edge, side), HALF - 1, "<"))
    if brow:
        rows.append(_put(_put("", edge, side), eye_at, brow))
    rows.append(_put(_put("", edge, side), eye_at, eye))
    cheeks = rng.choice((None, ".", "*", "'", None))
    if cheeks:
        rows.append(_put(_put("", edge, side), eye_at - 1, cheeks))
    mouth = MOUTHS[low]
    rows.append(_put(_put("", edge, side), HALF - len(mouth), mouth))
    rows.append(_put("", edge + (1 if head != "box" else 0), bottom_corner + bottom_fill * (width + 1 - len(bottom_corner) - (1 if head != "box" else 0))))

    # The body, with the net-span on the chest.
    chest = len(rows)
    if amphi:
        span = rng.choice((2, 3, 3))
        rows += [_put("", 0, "\\" + "_" * (edge - 1) + "\\" + " " * (width - 1)),
                 _put("", 1, "\\" + "/\\" * ((edge - 2) // 2) + " |"),
                 _put("", 3, "\\/" + " " * (edge - 4) + "|")][:span]
        rows.append(_put("", edge + 1, "/_" + "_" * (width - 3)))
    elif xeno:
        drift = rng.choice(("(", ")", "~"))
        rows += [_put("", edge + 1, "/" + " " * (width - 1)), _put("", edge, f"{drift} {drift}" + " " * (width - 3))]
        for depth in range(rng.choice((2, 3))):
            curl = rng.choice(("( (", ") )", "~ ~", "( )", ") ("))
            rows.append(_put("", edge - 1 + depth % 2, curl))
    else:
        rows += [_put("", edge, "/|" + " " * (width - 1)), _put("", edge - 1, "/ |" + " " * (width - 2))]
        rows.append(_put("", edge - 2, "/__|" + "_" * (width - 3)))
        if rng.random() < 0.6:
            rows.append(_put("", HALF - 2, "/" + rng.choice("o|*")))  # the pendulum
    art = [_mirror(row) for row in rows]
    line = chest + 1
    art[line] = art[line][:HALF - 2] + demon.span + art[line][HALF + 2:]  # "5::4": four characters, dead centre
    if demon.syzygy:  # the syzygy's own axis, through the figure from above to below
        art = [" " * (HALF - 1) + "::"] + art + [" " * (HALF - 1) + "::"]

    # Stars and drift in the empty space around the figure.
    stars = ".'*+." if not xeno else ".:;'`"
    for _ in range(rng.choice((3, 4, 5))):
        r, c = rng.randrange(len(art)), rng.randrange(0, 4)
        if art[r][c] == " " and art[r][-1 - c] == " ":
            mark = rng.choice(stars)
            art[r] = art[r][:c] + mark + art[r][c + 1:]
            if not xeno:
                art[r] = art[r][:len(art[r]) - 1 - c] + mark.translate(_MIRROR) + art[r][len(art[r]) - c:]
    return "\n".join(row.rstrip() for row in art)
