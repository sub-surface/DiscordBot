"""Build the #nanospasm quote bank (psychograph/outside.py) from the books' PDFs, in two steps.

    python tools/build_quote_bank.py candidates <vault Reading dir>   # data/quotes/candidates-<book>.jsonl
    python tools/build_quote_bank.py merge                            # picks-<book>.json -> data/quotes.json

`candidates` extracts each book with pdftotext, page by page, rejoins hyphenated lines, and keeps sentences that could
stand alone: 9-45 words, in English, no citation or footnote debris, not inside a paragraph that is someone else's words (one that
ends in a bracketed reference like [N III 542], or follows a lead-in ending in a colon). Each row has an id, the
printed page, the sentence and its paragraph. A curator (a Haiku agent) reads a candidates file and writes
picks-<book>.json: [{"id": ..., "theme": ...}] for the lines worth posting. `merge` joins the picks with their rows.

The books are in copyright: the bank stays in data/ (gitignored), and a post is one sentence with its source.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "quotes"
BANK = OUT.parent / "quotes.json"
BOOKS = {  # key -> (file, author, title, year)
    "fn": ("Land_Fanged_Noumena.pdf", "Nick Land", "Fanged Noumena", 2011),
    "tfa": ("Land Thirst_for_Annihilation_-_George_Bataille_and_Virulent_Nihilism.pdf", "Nick Land", "The Thirst for Annihilation", 1992),
    "dr": ("Deleuze - Difference and repetition.pdf", "Gilles Deleuze", "Difference and Repetition", 1968),
}
_CITED = re.compile(r"\[[^\]]{1,30}\d[^\]]{0,10}\]\.?\s*$")       # ends in a reference: a quotation
_DEBRIS = re.compile(r"\b(?:ibid|op\. cit|trans\.|pp?\.\s*\d|vol\.|ed\.|cf\.)|\(\d{4}\)|https?://|\[\w{0,4}\s?\d", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])[\"'”’)]*\s+(?=[A-Z\"'“‘(])")
_GERMAN = re.compile(r"\b(?:und|der|die|das|nicht|ist|sich|des|zu|mit|ein|eine|den|dem|von|auf)\b")
_NOTE_MARK = re.compile(r"\d{1,3}\s*$|\w,\d|\s,\d|['’]\s?,?\d")
_PAGE = re.compile(r"^\s*(?:\D{0,60}?\s)?(\d{1,3})(?:\s\D{0,60})?\s*$")


def pages(pdf: Path) -> list[str]:
    text = subprocess.run(["pdftotext", "-enc", "UTF-8", str(pdf), "-"], capture_output=True, check=True).stdout
    return text.decode("utf-8", "replace").split("\f")


def printed_page(page: str, index: int, offsets: dict[int, int]) -> int | None:
    """The page number printed in the header or footer, else the PDF index shifted by the book's usual offset."""
    lines = [line for line in page.splitlines() if line.strip()]
    for line in lines[:2] + lines[-2:]:
        match = _PAGE.match(line)
        if match and len(line) < 80:
            offsets[index - int(match.group(1))] = offsets.get(index - int(match.group(1)), 0) + 1
            return int(match.group(1))
    if offsets:
        return index - max(offsets, key=offsets.get)
    return None


def paragraphs(page: str) -> list[str]:
    """Blank-line-separated paragraphs, lines rejoined (and hyphenated words mended), short header lines dropped."""
    out = []
    for block in re.split(r"\n\s*\n", page):
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines or (len(lines) == 1 and len(lines[0]) < 60):
            continue  # running heads, page numbers, headings
        text = ""
        for line in lines:
            text = text[:-1] + line if text.endswith("-") and not text.endswith("--") else f"{text} {line}".strip()
        out.append(" ".join(text.split()))
    return out


def candidates(reading: Path) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for key, (file, *_rest) in BOOKS.items():
        rows, offsets, quoted_next = [], {}, False
        for index, page in enumerate(pages(reading / file), start=1):
            number = printed_page(page, index, offsets)
            for paragraph in paragraphs(page):
                someone_else = quoted_next or bool(_CITED.search(paragraph))
                quoted_next = paragraph.rstrip().endswith(":")  # "... Nietzsche notes that:" -> a block quote follows
                if someone_else:
                    continue
                for sentence in _SENTENCE.split(paragraph):
                    words = sentence.split()
                    if not 9 <= len(words) <= 45 or _DEBRIS.search(sentence) or not sentence[:1].isupper():
                        continue
                    if len(_GERMAN.findall(sentence)) >= 2 or _NOTE_MARK.search(sentence.rstrip(".")):
                        continue  # a German passage, or a footnote number left in the line
                    if not sentence.rstrip("\"'”’)").endswith((".", "!", "?")) or sentence.count("\"") % 2:
                        continue
                    if re.search(r"\b[A-Z](?: [A-Z]){3,}\b|\w \w \w \w\b", sentence):
                        continue  # OCR letter-spacing ("F A N G E D")
                    rows.append({"id": f"{key}-{len(rows):05d}", "page": number, "text": sentence, "paragraph": paragraph[:1500]})
        path = OUT / f"candidates-{key}.jsonl"
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        # What the curator reads: one "id<TAB>sentence" line each, without the paragraphs.
        (OUT / f"review-{key}.txt").write_text("".join(f"{row['id']}\t{row['text']}\n" for row in rows), encoding="utf-8")
        print(f"{key}: {len(rows)} candidates -> {path}")


_SHORT_WORDS = frozenset("a i an as at be by do go he if in is it me my no of on or so to up us we".split())


def _real(word: str, vocabulary: dict[str, int]) -> bool:
    word = word.lower()
    return word in _SHORT_WORDS if len(word) <= 2 else vocabulary.get(word, 0) >= 2


def mend(text: str, vocabulary: dict[str, int]) -> str:
    """Undo the scan's damage: soft hyphens at old line breaks, spaced punctuation, footnote numbers, a J read for
    an f or ']' for a J, words broken by stray spaces ("m orality", "b o dies": rejoined when the whole is a word of
    the book and a piece isn't), and words run together ("ofwork": split when rare and both halves are common)."""
    def unbreak(match: re.Match) -> str:  # a soft hyphen at an old line break: one word, or a real compound
        head, tail = match.group(1), match.group(2)
        compound = _real(head, vocabulary) and (
            tail.lower() in _SHORT_WORDS  # "capital-as-process"
            or (vocabulary.get((head + tail).lower(), 0) == 0 and vocabulary.get(tail.lower(), 0) >= 5))
        return f"{head}-{tail}" if compound else head + tail

    text = re.sub(r"([A-Za-z]+)­\s*([a-z]+)", unbreak, text)
    text = text.replace("­", "")
    text = re.sub(r"(?<![\w\]])\](?=[a-z])", "J", text)
    text = re.sub(r"\b([B-HJ-Z]) ([a-z]{2,})", r"\1\2", text)  # "S ocialism", "U mmnu" (A and I are words)
    text = re.sub(r"(?<=[.'’\"”])\d{1,3}\b", "", text)          # a footnote number after a full stop or quote
    text = re.sub(r"(?<=[a-z])J(?=[a-z])", "f", text)
    text = re.sub(r"\s*\.\s\.\s\.", "…", text)
    text = re.sub(r"(?<=[.…!?])\s\d{1,3}\s(?=[A-Z])", " ", text)
    text = re.sub(r"\s+([,;:.!?)])", r"\1", text)
    text = re.sub(r"([(])\s+", r"\1", text)
    tokens = text.split()
    out: list[str] = []
    index = 0
    while index < len(tokens):
        for span in (3, 2):  # the longest rejoin first
            pieces = tokens[index:index + span]
            if len(pieces) < span or not all(re.fullmatch(r"[A-Za-z]+", piece) for piece in pieces[:-1]):
                continue
            core = re.match(r"[A-Za-z]+", pieces[-1])
            if core is None:
                continue
            whole = "".join(pieces[:-1]) + core.group(0)
            if vocabulary.get(whole.lower(), 0) >= 1 and not all(_real(piece, vocabulary) for piece in pieces[:-1] + [core.group(0)]):
                out.append(whole + pieces[-1][core.end():])
                index += span
                break
        else:
            out.append(tokens[index])
            index += 1

    def split(match: re.Match) -> str:
        word = match.group(0)
        if vocabulary.get(word.lower(), 0) > 1:
            return word
        for cut in range(1, len(word) - 1):
            head, tail = word[:cut], word[cut:]
            if _real(head, vocabulary) and _real(tail, vocabulary) and vocabulary.get(tail.lower(), 0) >= 3 \
                    and (len(head) <= 3 or vocabulary.get(head.lower(), 0) >= 3):
                return f"{head} {tail}"
        return word

    return re.sub(r"[A-Za-z]{4,}", split, " ".join(out))


def merge() -> None:
    bank = []
    for key, (_file, author, title, year) in BOOKS.items():
        picks_path = OUT / f"picks-{key}.json"
        if not picks_path.exists():
            continue
        rows = {row["id"]: row for row in map(json.loads, (OUT / f"candidates-{key}.jsonl").read_text(encoding="utf-8").splitlines())}
        vocabulary: dict[str, int] = {}
        for row in rows.values():
            for word in re.findall(r"[a-z]+", re.sub(r"­\s*", "", row["paragraph"]).lower()):  # no "est" from "blind-est"
                vocabulary[word] = vocabulary.get(word, 0) + 1
        for row in rows.values():
            row["text"], row["paragraph"] = mend(row["text"], vocabulary), mend(row["paragraph"], vocabulary)
        picks = json.loads(picks_path.read_text(encoding="utf-8"))
        kept = [dict(rows[pick["id"]], theme=pick.get("theme", ""), author=author, title=title, year=year)
                for pick in picks if pick.get("id") in rows]
        bank.extend(kept)
        print(f"{key}: {len(kept)} of {len(picks)} picks matched")
    BANK.write_text(json.dumps({"quotes": bank}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(bank)} quotes -> {BANK}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["candidates"] and len(sys.argv) == 3:
        candidates(Path(sys.argv[2]))
    elif sys.argv[1:2] == ["merge"]:
        merge()
    else:
        sys.exit(__doc__)
