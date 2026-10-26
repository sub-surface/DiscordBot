"""Build the #nanospasm quote bank (psychograph/outside.py) from the books' PDFs, in two steps.

    python tools/build_quote_bank.py candidates <Reading dir> [key ...]   # data/quotes/candidates-<book>.jsonl
    python tools/build_quote_bank.py more <key> [key ...]                 # another 600 unseen lines, mended
    python tools/build_quote_bank.py merge                                # picks-<book>*.json -> data/quotes.json

`candidates` does the named books, or every book that has no candidates file yet (never redo one that has picks:
the ids would move under them).

`candidates` extracts each book with pdftotext, page by page, rejoins hyphenated lines, and keeps sentences that could
stand alone: 9-45 words, in English, no citation or footnote debris, not inside a paragraph that is someone else's words (one that
ends in a bracketed reference like [N III 542], or follows a lead-in ending in a colon). Each row has an id, the
printed page, the sentence and its paragraph. Runs of two or three sentences that make one point (25-90 words) are
candidates too, for the longer thoughts. The review file the curator reads is a sample: lines that lean on what came
before ("This means…", "However…") are dropped, and a long book is cut to REVIEW_LINES spread across it.
A curator (a Haiku agent) reads a review file and writes
picks-<book>.json: [{"id": ..., "theme": ...}] for the lines worth posting. `merge` joins the picks with their rows.

The books are in copyright: the bank stays in data/ (gitignored), and a post is one sentence with its source.
"""

from __future__ import annotations

import html
import json
import random
import re
import subprocess
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "data" / "quotes"
BANK = OUT.parent / "quotes.json"
BOOKS = {  # key (letters only, for the Deeper button's id) -> (file under the Reading dir, author, title, year; BC < 0)
    "fn": ("Land_Fanged_Noumena.pdf", "Nick Land", "Fanged Noumena", 2011),
    "tfa": ("Land Thirst_for_Annihilation_-_George_Bataille_and_Virulent_Nihilism.pdf", "Nick Land", "The Thirst for Annihilation", 1992),
    "dr": ("Deleuze - Difference and repetition.pdf", "Gilles Deleuze", "Difference and Repetition", 1968),
    "hegel": ("Books/Hegel - The Phenomenology of Spirit (Terry Pinkard Translation).pdf", "G. W. F. Hegel", "Phenomenology of Spirit", 1807),
    "kant": ("People/Kant/kant critique of pure reason.pdf", "Immanuel Kant", "Critique of Pure Reason", 1781),
    "bge": ("People/Nietzsche/Nietzsche-Beyond-Good-and-Evil.pdf", "Friedrich Nietzsche", "Beyond Good and Evil", 1886),
    "twi": ("People/Nietzsche/Friedrich-Nietzsche-Twilight-of-the-Idols.pdf", "Friedrich Nietzsche", "Twilight of the Idols", 1889),
    "gs": ("People/Nietzsche/The-Gay-Science-by-Friedrich-Nietzsche.pdf", "Friedrich Nietzsche", "The Gay Science", 1882),
    "zara": ("People/Nietzsche/Thus spoke Zarathustra.pdf", "Friedrich Nietzsche", "Thus Spoke Zarathustra", 1883),
    "ecce": ("People/Nietzsche/Ecce Homo.pdf", "Friedrich Nietzsche", "Ecce Homo", 1908),
    "wtp": ("Books/friedrich_nietzsche the_will_to_power_1968.pdf", "Friedrich Nietzsche", "The Will to Power", 1901),
    "spinoza": ("People/Spinoza/Cambridge Texts In The History Of Philosophy Benedict de Spinoza Baruch Spinoza Matthew J Kisner Michael - Ethics_ Proved In Geometrical Order 2018 Cambridge University Press - libgenli.pdf", "Baruch Spinoza", "Ethics", 1677),
    "plato": ("the-complete-dialogues-of-plato.pdf", "Plato", "Dialogues", -380),
    "simp": ("Simplicius Commentary on Epictetus' Handbook.pdf", "Simplicius", "Commentary on Epictetus' Handbook", 530),
    "wit": ("wittgenstein on-certainty.pdf", "Ludwig Wittgenstein", "On Certainty", 1969),
    "voe": ("Books/georges-bataille-visions-of-excess-selected-writings-19271939.pdf", "Georges Bataille", "Visions of Excess", 1985),
    "eye": ("bataille_story_of_eye.pdf", "Georges Bataille", "Story of the Eye", 1928),
    "sim": ("People/Baudrillard/Baudrillard Simulacra_and_Simulation.pdf", "Jean Baudrillard", "Simulacra and Simulation", 1981),
    "cr": ("People/Mark Fisher/Fisher_Capitalist Realism.pdf", "Mark Fisher", "Capitalist Realism", 2009),
    "pcd": ("People/Mark Fisher/Fisher postcapitalist desire - the final lectures.pdf", "Mark Fisher", "Postcapitalist Desire", 2020),
    "flat": ("People/Mark Fisher/Fisher flatline-constructs.pdf", "Mark Fisher", "Flatline Constructs", 1999),
    "ill": ("Books/benjamin illuminations.pdf", "Walter Benjamin", "Illuminations", 1955),
    "qct": ("Heidegger Martin_The_Question_Concerning_Technology_and_Other_Essays.pdf", "Martin Heidegger", "The Question Concerning Technology", 1954),
    "bt": ("Books/Heidegger_Being-and-Time-trans-Macquarrie-Robinson-Blackwell-1962.pdf", "Martin Heidegger", "Being and Time", 1927),
    "mm": ("People/Bergson/Bergson Matter and Memory.pdf", "Henri Bergson", "Matter and Memory", 1896),
    "tp": ("People/Deleuze & Guattari/A Thousand Plateaus.pdf", "Gilles Deleuze and Félix Guattari", "A Thousand Plateaus", 1980),
    "ao": ("People/Deleuze & Guattari/Anti-Oedipus.pdf", "Gilles Deleuze and Félix Guattari", "Anti-Oedipus", 1972),
    "wip": ("People/Deleuze & Guattari/Deleuze and Guattari What Is Philosophy.pdf", "Gilles Deleuze and Félix Guattari", "What Is Philosophy?", 1991),
    "np": ("People/Deleuze & Guattari/Deleuze nietzsche-and-philosophy.pdf", "Gilles Deleuze", "Nietzsche and Philosophy", 1962),
    "neg": ("People/Deleuze & Guattari/Deleuze - Negotiations.pdf", "Gilles Deleuze", "Negotiations", 1990),
    "psc": ("People/Deleuze & Guattari/Deleuze PostscriptSocietiesControl-1992.pdf", "Gilles Deleuze", "Postscript on the Societies of Control", 1990),
    "ccru": ("People/Nick Land/Ccru_Writings_1997-2003.pdf", "Ccru", "Writings 1997–2003", 2015),
    "xs": ("People/Nick Land/LAND XENOSYSTEMS_FRAGMENTS.pdf", "Nick Land", "Xenosystems Fragments", 2017),
    "md": ("People/Nick Land/Land machinicdesire.pdf", "Nick Land", "Machinic Desire", 1993),
    "nu": ("Speculative Realism/Brassier nihil-unbound-enlightenment-and-extinction.pdf", "Ray Brassier", "Nihil Unbound", 2007),
    "ias": ("Speculative Realism/Negarestani Intelligence And Spirit.pdf", "Reza Negarestani", "Intelligence and Spirit", 2018),
    "af": ("Speculative Realism/meillassoux after-finitude academic_continuum-2009.pdf", "Quentin Meillassoux", "After Finitude", 2006),
    "sl": ("People/Hofstadter/Douglas_Hofstadter_I_Am_a_Strange_Loop.pdf", "Douglas Hofstadter", "I Am a Strange Loop", 2007),
    "geb": ("People/Hofstadter/Hofstadter_GEB.pdf", "Douglas Hofstadter", "Gödel, Escher, Bach", 1979),
    "dod": ("Ernest-Becker-The-Denial-of-Death.pdf", "Ernest Becker", "The Denial of Death", 1973),
    "bpp": ("Books/freud_beyond_the_pleasure_principle.pdf", "Sigmund Freud", "Beyond the Pleasure Principle", 1920),
    "ti": ("Books/Levinas Totality And Infinity_text.pdf", "Emmanuel Levinas", "Totality and Infinity", 1961),
    "le": ("Books/Lyotard Libidinal_Economy.pdf", "Jean-François Lyotard", "Libidinal Economy", 1974),
    "dp": ("Books/Foucault Discipline_and_Punish_The_Birth_of_the_Prison_1977_1995.pdf", "Michel Foucault", "Discipline and Punish", 1975),
    "hs": ("foucault the-history-of-sexuality-volume-1.pdf", "Michel Foucault", "The History of Sexuality, Vol. 1", 1976),
    "so": ("People/Slavoj Zizek/slavoj-zizek-the-sublime-object-of-ideology-second-edition-the-essential-zizek-2009.pdf", "Slavoj Žižek", "The Sublime Object of Ideology", 1989),
    "ltn": ("People/Slavoj Zizek/slavoj-zizek-less-than-nothing-hegel-and-the-shadow-of-dialectical-materialism-theoryreader.pdf", "Slavoj Žižek", "Less Than Nothing", 2012),
    "pr": ("Books/Whitehead_process-and-reality.pdf", "Alfred North Whitehead", "Process and Reality", 1929),
    "gg": ("Books/mcluhan-marshall_the-gutenberg-galaxy-the-making-of-typographic-man.pdf", "Marshall McLuhan", "The Gutenberg Galaxy", 1962),
    "um": ("People/Graham Harman/understanding-media-mcluhan.pdf", "Marshall McLuhan", "Understanding Media", 1964),
    "oc": ("Books/Jaynes_The Origin of Consciousness in the Breakdown of the Bicameral Mind_ 1976.pdf", "Julian Jaynes", "The Origin of Consciousness in the Breakdown of the Bicameral Mind", 1976),
    "ce": ("Books/Dennett Consciousness-Explained.pdf", "Daniel Dennett", "Consciousness Explained", 1991),
    "et": ("metzinger egotunnel.pdf", "Thomas Metzinger", "The Ego Tunnel", 2009),
    "hbp": ("Books/Hayles_How we became posthuman (1999).pdf", "N. Katherine Hayles", "How We Became Posthuman", 1999),
    "som": ("Books/Derrida - Specters of Marx.pdf", "Jacques Derrida", "Specters of Marx", 1993),
    "be": ("Books/Badiou Being_and_Event_Searchable.pdf", "Alain Badiou", "Being and Event", 1988),
    "vm": ("Books/Banette vibrant-matter.pdf", "Jane Bennett", "Vibrant Matter", 2010),
    "ap": ("Speculative Realism/Bogost Alien-Phenomenology.pdf", "Ian Bogost", "Alien Phenomenology", 2012),
    "dph": ("Colebrook 2014_Death-of-the-PostHuman.pdf", "Claire Colebrook", "Death of the PostHuman", 2014),
    "noise": ("Books/Wilkins_Irreversible_Noise.pdf", "Inigo Wilkins", "Irreversible Noise", 2023),
    "nsl": ("Shaviro No Speed Limit - Three Essays on Accelerationism.pdf", "Steven Shaviro", "No Speed Limit", 2015),
    "inm": ("Kahane If nothing matters.pdf", "Guy Kahane", "If Nothing Matters", 2017),
    "zo": ("Sadie Plant Zeros_and_Ones_Digital_Women_and_the_New_Technoculture_1997.pdf", "Sadie Plant", "Zeros + Ones", 1997),
    "mrg": ("Books/Plant Sadie The_Most_Radical_Gesture_The_Situationist_International_in_a_Postmodern_Age.pdf", "Sadie Plant", "The Most Radical Gesture", 1992),
    "cm": ("Articles/Other/Haraway_Cyborg_Manifesto_1984.pdf", "Donna Haraway", "A Cyborg Manifesto", 1985),
    "xf": ("xenofeminism_manifesto.pdf", "Laboria Cuboniks", "Xenofeminism", 2015),
    "flus": ("People/Flusser/Flusser_TowardsAPhilosophyofPhotography.pdf", "Vilém Flusser", "Towards a Philosophy of Photography", 1983),
    "di": ("CS LEWIS Discarded Image.pdf", "C. S. Lewis", "The Discarded Image", 1964),
    "war": ("Books/Chris Hedges - War Is a Force That Gives Us Meaning-PublicAffairs (2002).pdf", "Chris Hedges", "War Is a Force That Gives Us Meaning", 2002),
    "bsj": ("graeber bullshit-jobs.pdf", "David Graeber", "Bullshit Jobs", 2018),
    "eno": ("Books/Brian-Eno_A_Year_With_Swollen_Appendices_Brian_Enos_Diary.pdf", "Brian Eno", "A Year with Swollen Appendices", 1996),
    "proust": ("Books/Proust lost-time.pdf", "Marcel Proust", "In Search of Lost Time", 1913),
    "bm": ("Books/Cormac Mccarthy - The Blood Meridian.pdf", "Cormac McCarthy", "Blood Meridian", 1985),
    "obl": ("People/David Foster Wallace/David Foster Wallace Oblivion stories.pdf", "David Foster Wallace", "Oblivion", 2004),
    "neu": ("Gibson neuromancer.pdf", "William Gibson", "Neuromancer", 1984),
    "trakl": ("Other/Trakl - 20 poems.pdf", "Georg Trakl", "Poems", 1913),
}
_CITED = re.compile(r"\[[^\]]{1,30}\d[^\]]{0,10}\]\.?\s*$")       # ends in a reference: a quotation
_DEBRIS = re.compile(r"\b(?:ibid|op\. cit|trans\.|pp?\.\s*\d|vol\.|ed\.|cf\.)|\(\d{4}\)|https?://|\[\w{0,4}\s?\d", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])[\"'”’)]*\s+(?=[A-Z\"'“‘(])")
_GERMAN = re.compile(r"\b(?:und|der|die|das|nicht|ist|sich|des|zu|mit|ein|eine|den|dem|von|auf)\b")
_NOTE_MARK = re.compile(r"\d{1,3}\s*$|\w,\d|\s,\d|['’]\s?,?\d")
# A line that opens by pointing back at what came before reads as a fragment of a fragment.
_LEANS_BACK = re.compile(
    r"(?:This|That|These|Those|It|Its|His|Her|She|They|Their|Them|Such|But|And|Or|Yet|So|Thus|Hence|Therefore|However|"
    r"Here|Moreover|Furthermore|Also|Then|Again|Similarly|Likewise|Instead|Nevertheless|Accordingly|Consequently|"
    r"Indeed|In (?:this|that|other words|short|turn|fact|the same|both)|For (?:this|that|example|instance)|"
    r"On the (?:other|one) hand|As (?:we|I) (?:have|saw|shall)|We (?:have|shall|will) (?:see|seen)|I (?:have|shall|will)|"
    r"See|Cf|Figure|Table|Chapter|Section|Above|Below)\b")
REVIEW_LINES = 400
_MISREAD = re.compile(r"[\u1100-\u11ff\u3000-\u9fff\uac00-\ud7af\ufffd]")  # a font the scan read as Hangul or CJK
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


def clean_sentence(sentence: str, shortest: int = 9) -> bool:
    """Whether a sentence is whole and free of scan and citation debris (and has `shortest` to 45 words)."""
    words = sentence.split()
    if not shortest <= len(words) <= 45 or _DEBRIS.search(sentence) or not sentence[:1].isupper():
        return False
    if len(_GERMAN.findall(sentence)) >= 2 or _NOTE_MARK.search(sentence.rstrip(".")):
        return False  # a German passage, or a footnote number left in the line
    if not sentence.rstrip("\"'”’)").endswith((".", "!", "?")) or sentence.count("\"") % 2:
        return False
    return not re.search(r"\b[A-Z](?: [A-Z]){3,}\b|\w \w \w \w\b", sentence)  # OCR letter-spacing ("F A N G E D")


def candidates(reading: Path, keys: list[str] | None = None) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for key, (file, *_rest) in BOOKS.items():
        if keys and key not in keys or not keys and (OUT / f"candidates-{key}.jsonl").exists():
            continue
        if not (reading / file).exists():
            print(f"{key}: missing {file}")
            continue
        rows, offsets, quoted_next = [], {}, False
        for index, page in enumerate(pages(reading / file), start=1):
            number = printed_page(page, index, offsets)
            for paragraph in paragraphs(page):
                someone_else = quoted_next or bool(_CITED.search(paragraph))
                quoted_next = paragraph.rstrip().endswith(":")  # "... Nietzsche notes that:" -> a block quote follows
                if someone_else:
                    continue
                sentences = _SENTENCE.split(paragraph)
                for sentence in sentences:
                    if clean_sentence(sentence):
                        rows.append({"id": f"{key}-{len(rows):05d}", "page": number, "text": sentence, "paragraph": paragraph[:1500]})
                for start in range(len(sentences)):  # the longer thoughts: two or three sentences that run together
                    for size in (2, 3):
                        run = sentences[start:start + size]
                        if len(run) == size and 25 <= len(" ".join(run).split()) <= 90 and all(clean_sentence(s, 4) for s in run):
                            rows.append({"id": f"{key}-{len(rows):05d}", "page": number, "text": " ".join(run), "paragraph": paragraph[:1500]})
                            break
        path = OUT / f"candidates-{key}.jsonl"
        path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        # What the curator reads: one "id<TAB>text" line each, without the paragraphs, sampled.
        review = [row for row in rows if not _LEANS_BACK.match(row["text"])]
        if len(review) > REVIEW_LINES:
            review = sorted(random.Random(key).sample(review, REVIEW_LINES), key=lambda row: row["id"])
        (OUT / f"review-{key}.txt").write_text("".join(f"{row['id']}\t{row['text']}\n" for row in review), encoding="utf-8")
        print(f"{key}: {len(rows)} candidates, {len(review)} to review -> {path}")


_LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st"})
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

    text = html.unescape(text)                                   # "&quot;" left in by an ebook's conversion
    text = text.translate(_LIGATURES)                            # "ﬁnal" -> "final"
    text = re.sub(r"^[A-Z][A-Z .]{2,24}:\s+", "", text)          # a dialogue's speaker ("SOCRATES: ...")
    text = re.sub(r"(?<=[a-z])\ufffd\s*(?=[a-z])", "", text)  # an unreadable soft hyphen inside a word ("res\ufffd pects")
    text = re.sub(r"\s*\ufffd\s*(?:[a-z]\.\s+)?", " ", text)   # a stray one, with any list letter after it ("\ufffd b.")
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


def mended_rows(key: str) -> dict[str, dict]:
    """A book's candidates by id, with the scan damage mended (as the bank will show them)."""
    rows = {row["id"]: row for row in map(json.loads, (OUT / f"candidates-{key}.jsonl").read_text(encoding="utf-8").splitlines())}
    vocabulary: dict[str, int] = {}
    for row in rows.values():
        for word in re.findall(r"[a-z]+", re.sub(r"­\s*", "", row["paragraph"]).lower()):  # no "est" from "blind-est"
            vocabulary[word] = vocabulary.get(word, 0) + 1
    for row in rows.values():
        row["text"], row["paragraph"] = mend(row["text"], vocabulary), mend(row["paragraph"], vocabulary)
    return rows


def more(keys: list[str], lines: int = 600) -> None:
    """Another review file for a book (review-<key>-2.txt, -3, …): lines no review file has shown yet, mended, so a
    curator isn't put off by damage the merge repairs anyway. Its picks go in picks-<key>-2.json and so on."""
    for key in keys:
        seen = {line.split("\t", 1)[0] for path in OUT.glob(f"review-{key}*.txt")
                for line in path.read_text(encoding="utf-8").splitlines()}
        rows = [row for row in mended_rows(key).values() if row["id"] not in seen
                and not _LEANS_BACK.match(row["text"]) and not _MISREAD.search(row["text"])]
        if len(rows) > lines:
            rows = sorted(random.Random(f"{key}-more").sample(rows, lines), key=lambda row: row["id"])
        number = 2 + sum(1 for _ in OUT.glob(f"review-{key}-*.txt"))
        (OUT / f"review-{key}-{number}.txt").write_text("".join(f"{row['id']}\t{row['text']}\n" for row in rows), encoding="utf-8")
        print(f"{key}: {len(rows)} more to review -> review-{key}-{number}.txt")


def merge() -> None:
    bank = []
    for key, (_file, author, title, year) in BOOKS.items():
        paths = sorted(OUT.glob(f"picks-{key}.json")) + sorted(OUT.glob(f"picks-{key}-*.json"))
        if not paths:
            continue
        rows = mended_rows(key)
        picks = [pick for path in paths for pick in json.loads(path.read_text(encoding="utf-8"))]
        kept = [dict(rows[pick["id"]], theme=pick.get("theme", ""), author=author, title=title, year=year)
                for pick in picks if pick.get("id") in rows and not _MISREAD.search(rows[pick["id"]]["text"])]
        # A passage and a sentence inside it can both be picked: keep the shorter line (Deeper gives the rest).
        unique: list[dict] = []
        for row in sorted(kept, key=lambda row: len(row["text"])):
            if not any(other["text"] in row["text"] for other in unique) and row["id"] not in {u["id"] for u in unique}:
                unique.append(row)
        kept = sorted(unique, key=lambda row: row["id"])
        bank.extend(kept)
        print(f"{key}: {len(kept)} of {len(picks)} picks matched")
    BANK.write_text(json.dumps({"quotes": bank}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(bank)} quotes -> {BANK}")


if __name__ == "__main__":
    if sys.argv[1:2] == ["candidates"] and len(sys.argv) >= 3:
        candidates(Path(sys.argv[2]), sys.argv[3:])
    elif sys.argv[1:2] == ["more"] and len(sys.argv) >= 3:
        more(sys.argv[2:])
    elif sys.argv[1:2] == ["merge"]:
        merge()
    else:
        sys.exit(__doc__)
