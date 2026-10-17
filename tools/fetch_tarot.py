"""Download the 1909 Rider-Waite-Smith tarot (public domain) from Wikimedia Commons into assets/tarot/.

Saved as <key>.jpg at 960 px wide (a Commons standard thumbnail size; others are throttled): major arcana as 00-fool … 21-world, minors as cups-01 … wands-14
(11 page, 12 knight, 13 queen, 14 king). Run once: `python tools/fetch_tarot.py`.
"""

from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CATEGORY = "Category:Rider-Waite tarot deck (Roses & Lilies)"
OUT = Path(__file__).resolve().parent.parent / "assets" / "tarot"
HEADERS = {"User-Agent": "PsychographDiscordBot/1.0 (tarot deck, one-off fetch)"}
_MAJOR = re.compile(r"RWS1909 - (\d\d) (.+)\.jpe?g$")
_MINOR = re.compile(r"RWS1909 - (Cups|Pentacles|Swords|Wands) (\d\d)\.jpe?g$")


def api(**params: str) -> dict:
    url = "https://commons.wikimedia.org/w/api.php?" + urllib.parse.urlencode({**params, "format": "json"})
    with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=30) as response:
        return json.load(response)


def key_for(title: str) -> str | None:
    if match := _MAJOR.search(title):
        return f"{match.group(1)}-{match.group(2).lower().replace(' ', '-')}"
    if match := _MINOR.search(title):
        return f"{match.group(1).lower()}-{match.group(2)}"
    return None


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    titles, cont = [], {}
    while True:
        result = api(action="query", list="categorymembers", cmtitle=CATEGORY, cmtype="file", cmlimit="500", **cont)
        titles += [member["title"] for member in result["query"]["categorymembers"]]
        if "continue" not in result:
            break
        cont = result["continue"]
    cards = {key: title for title in titles if (key := key_for(title))}
    print(f"{len(cards)} cards")
    urls: dict[str, str] = {}
    by_title = {title: key for key, title in cards.items()}
    pending = [title for key, title in sorted(cards.items()) if not (OUT / f"{key}.jpg").exists()]
    for start in range(0, len(pending), 40):  # one API call per 40 cards
        info = api(action="query", titles="|".join(pending[start:start + 40]), prop="imageinfo", iiprop="url", iiurlwidth="960")
        for page in info["query"]["pages"].values():
            image = page["imageinfo"][0]
            urls[by_title[page["title"]]] = image.get("thumburl") or image["url"]
    for key, url in sorted(urls.items()):
        for attempt in range(6):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS), timeout=60) as response:
                    (OUT / f"{key}.jpg").write_bytes(response.read())
                break
            except urllib.error.HTTPError as error:
                if error.code != 429:
                    raise
                wait = int(error.headers.get("Retry-After") or 0) or 10 * (attempt + 1)
                print(f"429, waiting {wait}s", flush=True)
                time.sleep(wait)
        print(key, flush=True)
        time.sleep(3)

if __name__ == "__main__":
    sys.exit(main())
