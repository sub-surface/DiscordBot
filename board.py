import io
import os
import platform

# Unicode chess piece symbols
_PIECES = {
    'K': '\u2654', 'Q': '\u2655', 'R': '\u2656', 'B': '\u2657', 'N': '\u2658', 'P': '\u2659',
    'k': '\u265A', 'q': '\u265B', 'r': '\u265C', 'b': '\u265D', 'n': '\u265E', 'p': '\u265F',
}

_SQ      = 60          # pixels per square
_BORDER  = 20          # label border width
_SIZE    = _SQ * 8 + _BORDER * 2

_LIGHT  = (240, 217, 181)   # chess.com light square
_DARK   = (181, 136,  99)   # chess.com dark square
_BG     = ( 49,  46,  43)   # border background
_LABELS = (180, 162, 140)   # rank/file label text

def _get_font(names: list[str], size: int):
    from PIL import ImageFont
    # Common paths for different OS
    search_dirs = []
    if platform.system() == "Windows":
        search_dirs.append("C:/Windows/Fonts")
    elif platform.system() == "Darwin":
        search_dirs.extend(["/Library/Fonts", "/System/Library/Fonts", "~/Library/Fonts"])
    else:
        search_dirs.extend(["/usr/share/fonts", "/usr/local/share/fonts", "~/.fonts"])

    for name in names:
        # Try direct name (Pillow might find it if installed)
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            pass
        # Try common paths
        for d in search_dirs:
            full_path = os.path.join(os.path.expanduser(d), name)
            if os.path.exists(full_path):
                try:
                    return ImageFont.truetype(full_path, size)
                except Exception:
                    continue
    return ImageFont.load_default()

def fen_to_image(fen: str) -> bytes | None:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None

    try:
        rows = fen.strip().split()[0].split('/')
        if len(rows) != 8:
            return None

        img  = Image.new('RGB', (_SIZE, _SIZE), _BG)
        draw = ImageDraw.Draw(img)

        for rank in range(8):
            for file in range(8):
                x = _BORDER + file * _SQ
                y = _BORDER + rank * _SQ
                draw.rectangle(
                    [x, y, x + _SQ, y + _SQ],
                    fill=_LIGHT if (rank + file) % 2 == 0 else _DARK,
                )

        # Piece font: looking for fonts with good unicode coverage
        pf = _get_font(['seguisym.ttf', 'DejaVuSans.ttf', 'FreeSerif.ttf', 'Arial Unicode.ttf'], int(_SQ * 0.75))
        # Label font
        lf = _get_font(['arialbd.ttf', 'DejaVuSans-Bold.ttf', 'FreeSans-Bold.ttf', 'Helvetica-Bold.ttf'], 11)

        for i, f in enumerate('abcdefgh'):
            cx = _BORDER + i * _SQ + _SQ // 2
            draw.text((cx, _BORDER // 2),         f, font=lf, fill=_LABELS, anchor='mm')
            draw.text((cx, _SIZE - _BORDER // 2), f, font=lf, fill=_LABELS, anchor='mm')
        for r in range(8):
            cy  = _BORDER + r * _SQ + _SQ // 2
            lbl = str(8 - r)
            draw.text((_BORDER // 2,         cy), lbl, font=lf, fill=_LABELS, anchor='mm')
            draw.text((_SIZE - _BORDER // 2, cy), lbl, font=lf, fill=_LABELS, anchor='mm')

        def _draw_piece(cx: int, cy: int, ch: str) -> None:
            is_white = ch.isupper()
            fill    = (255, 255, 255) if is_white else ( 20,  20,  20)
            outline = ( 50,  50,  50) if is_white else (210, 200, 185)
            glyph   = _PIECES.get(ch, ch)
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (-1, -1), (1, -1), (-1, 1), (1, 1)):
                draw.text((cx + dx, cy + dy), glyph, font=pf, fill=outline, anchor='mm')
            draw.text((cx, cy), glyph, font=pf, fill=fill, anchor='mm')

        valid = set('KQRBNPkqrbnp')
        for ri, row in enumerate(rows):
            fi = 0
            for ch in row:
                if ch.isdigit():
                    fi += int(ch)
                else:
                    if ch in valid:
                        _draw_piece(_BORDER + fi * _SQ + _SQ // 2, _BORDER + ri * _SQ + _SQ // 2, ch)
                    fi += 1

        buf = io.BytesIO()
        img.save(buf, format='PNG', optimize=True)
        return buf.getvalue()
    except Exception:
        return None

def fen_to_board(fen: str) -> str:
    try:
        rows = fen.strip().split()[0].split('/')
        if len(rows) != 8: return ''
        lines = ['  a b c d e f g h']
        for i, row in enumerate(rows):
            cells = []
            for ch in row:
                if ch.isdigit(): cells.extend(['.'] * int(ch))
                else: cells.append(_PIECES.get(ch, '?'))
            lines.append(f'{8 - i} {" ".join(cells)}')
        return '```\n' + '\n'.join(lines) + '\n```'
    except Exception:
        return ''
