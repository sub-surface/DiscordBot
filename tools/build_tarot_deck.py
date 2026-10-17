"""Build assets/tarot/deck.json: the 78-card 1909 Rider-Waite-Smith deck, linked rather than downloaded.

Each card has its name, arcana, the full-resolution public-domain scan on Wikimedia Commons (Discord fetches and
caches it itself, so the bot never hosts or downloads the art), and short upright/reversed meanings after
A. E. Waite's "The Pictorial Key to the Tarot" (1910, public domain). Run once: `python tools/build_tarot_deck.py`.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "assets" / "tarot" / "deck.json"
HEADERS = {"User-Agent": "PsychographDiscordBot/1.0 (tarot deck metadata)"}

MAJORS = [  # (name, upright, reversed)
    ("The Fool", "a leap, beginnings, folly that turns out brave", "recklessness, a fall you could have seen"),
    ("The Magician", "skill, will, the tools all on the table", "trickery, talent spent on the wrong thing"),
    ("The High Priestess", "secrets, intuition, what isn't said", "surface knowledge, a secret let slip"),
    ("The Empress", "abundance, comfort, things growing", "smothering, plenty gone stale"),
    ("The Emperor", "order, rule, a firm hand", "tyranny, rules for their own sake"),
    ("The Hierophant", "tradition, institutions, the proper way", "orthodoxy questioned, a rule broken"),
    ("The Lovers", "a choice of the heart, a bond", "a bad match, a choice dodged"),
    ("The Chariot", "victory by force of will, momentum", "losing control, pulling two ways"),
    ("Strength", "courage, patience, gentle force", "self-doubt, force without grace"),
    ("The Hermit", "solitude, study, a light for the road", "isolation, refusing counsel"),
    ("Wheel of Fortune", "luck turns, fate, a new cycle", "bad luck, a cycle repeating"),
    ("Justice", "fairness, a verdict, consequences", "bias, a verdict that won't hold"),
    ("The Hanged Man", "suspension, sacrifice, a new angle", "stalling, sacrifice for nothing"),
    ("Death", "an ending that clears the way", "clinging to what's already over"),
    ("Temperance", "balance, moderation, the middle way", "excess, things out of proportion"),
    ("The Devil", "temptation, chains you chose", "breaking free, seeing the chains"),
    ("The Tower", "sudden upheaval, a revelation", "disaster narrowly avoided, or delayed"),
    ("The Star", "hope, renewal, a calm after", "despair, hope misplaced"),
    ("The Moon", "illusion, fear, the unclear path", "confusion lifting, a lie exposed"),
    ("The Sun", "joy, success, everything in the light", "joy delayed, success with a catch"),
    ("Judgement", "a reckoning, a calling, rebirth", "self-doubt, refusing the call"),
    ("The World", "completion, arrival, the whole picture", "unfinished business, a last step left"),
]
MAJOR_FILES = ["Fool", "Magician", "High_Priestess", "Empress", "Emperor", "Hierophant", "Lovers", "Chariot", "Strength",
               "Hermit", "Wheel_of_Fortune", "Justice", "Hanged_Man", "Death", "Temperance", "Devil", "Tower", "Star",
               "Moon", "Sun", "Judgement", "World"]
SUITS = {  # file prefix → (suit name, domain)
    "Wands": ("Wands", "will, work and ambition"),
    "Cups": ("Cups", "feeling, love and friendship"),
    "Swords": ("Swords", "conflict, thought and trouble"),
    "Pents": ("Pentacles", "money, body and the material world"),
}
RANKS = ["Ace", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Page", "Knight", "Queen", "King"]
MINORS = {  # suit → 14 × (upright, reversed)
    "Wands": [
        ("a spark, a new venture", "a false start"), ("planning, the world in your hand", "fear of the unknown"),
        ("ships coming in, foresight", "help that doesn't arrive"), ("celebration, a homecoming", "the party ends"),
        ("strife, a scrappy contest", "a fight avoided, or dirty"), ("victory, public acclaim", "pride before a fall"),
        ("holding the high ground", "overwhelmed, giving way"), ("speed, news arriving", "delay, frustration"),
        ("battered but standing guard", "exhaustion, paranoia"), ("burdens, too much carried", "putting the load down"),
        ("an eager messenger", "bad news, an unready envoy"), ("a dash into action", "haste, a reckless ride"),
        ("warmth, confident charm", "jealousy, a temper"), ("a bold leader, vision", "arrogance, impulsive rule"),
    ],
    "Cups": [
        ("an overflowing heart", "feelings held back"), ("a bond, a pledge", "a rift between two"),
        ("friends, a toast", "gossip, three's a crowd"), ("boredom, an offer ignored", "waking up to what's offered"),
        ("loss, crying over spilt wine", "acceptance, moving on"), ("nostalgia, innocence", "living in the past"),
        ("daydreams, too many choices", "clarity, a choice made"), ("walking away", "staying where you shouldn't"),
        ("a wish granted, smugness", "greed, the wish sours"), ("contentment, home", "a happy picture cracks"),
        ("a sweet surprise", "naivety, a mood"), ("a romantic offer", "a charming liar"),
        ("compassion, intuition", "moodiness, martyrdom"), ("calm mastery of feeling", "manipulation, coldness"),
    ],
    "Swords": [
        ("clarity, a breakthrough", "confusion, a blunted edge"), ("stalemate, a hard choice", "the truth comes out"),
        ("heartbreak, sorrow", "recovery, healing"), ("rest, a truce", "restlessness, burnout"),
        ("a hollow win, spite", "making amends"), ("passage to calmer water", "a trip that goes badly"),
        ("stealth, a sly plan", "caught out, a confession"), ("trapped by your own thinking", "release, seeing the way out"),
        ("anxiety, nightmares", "the worst passes"), ("rock bottom, it's over", "recovery begins"),
        ("vigilance, a sharp tongue", "spying, all talk"), ("a charge headlong", "a crash, no plan"),
        ("perceptive, blunt honesty", "bitterness, cruelty"), ("authority, judgement", "a tyrant, abuse of power"),
    ],
    "Pents": [
        ("a windfall, a new opportunity", "money slipping away"), ("juggling, adaptability", "dropping the ball"),
        ("craft, teamwork", "shoddy work"), ("holding on tight, thrift", "greed, or letting go"),
        ("hardship, out in the cold", "help found, the worst over"), ("generosity, charity", "strings attached"),
        ("patience, waiting on a harvest", "impatience, a poor return"), ("diligence, mastery", "cutting corners"),
        ("comfort earned alone", "a gilded cage"), ("wealth, family, legacy", "a fight over the will"),
        ("study, a careful student", "laziness, all plan no work"), ("slow, steady effort", "stuck in a rut"),
        ("practical care, plenty", "neglect, smothering"), ("prosperity, a solid ruler", "greed, corruption"),
    ],
}


def originals(titles: list[str]) -> dict[str, tuple[str, int, int]]:
    found = {}
    for start in range(0, len(titles), 50):
        query = urllib.parse.urlencode({"action": "query", "titles": "|".join(titles[start:start + 50]), "prop": "imageinfo",
                                        "iiprop": "url|size", "format": "json"})
        request = urllib.request.Request(f"https://commons.wikimedia.org/w/api.php?{query}", headers=HEADERS)
        with urllib.request.urlopen(request, timeout=30) as response:
            for page in json.load(response)["query"]["pages"].values():
                info = page["imageinfo"][0]
                found[page["title"].replace(" ", "_")] = (info["url"].split("?")[0], info["width"], info["height"])
    return found


def main() -> None:
    cards = []
    for number, ((name, up, down), file) in enumerate(zip(MAJORS, MAJOR_FILES)):
        cards.append({"key": f"major-{number:02d}", "name": name, "arcana": "major", "number": number,
                      "file": f"File:RWS_Tarot_{number:02d}_{file}.jpg", "upright": up, "reversed": down})
    for prefix, (suit, domain) in SUITS.items():
        for rank, (up, down) in zip(RANKS, MINORS[prefix]):
            index = RANKS.index(rank) + 1
            cards.append({"key": f"{suit.lower()}-{index:02d}", "name": f"{rank} of {suit}", "arcana": "minor", "suit": suit,
                          "domain": domain, "number": index, "file": f"File:{prefix}{index:02d}.jpg", "upright": up, "reversed": down})
    urls = originals([card["file"] for card in cards])
    for card in cards:
        card["image"], card["width"], card["height"] = urls[card.pop("file")]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"source": "1909 Rider-Waite-Smith deck, public domain, via Wikimedia Commons; meanings after "
                                         "Waite, The Pictorial Key to the Tarot (1910)", "cards": cards}, indent=1), encoding="utf-8")
    print(f"{len(cards)} cards -> {OUT}")


if __name__ == "__main__":
    main()
