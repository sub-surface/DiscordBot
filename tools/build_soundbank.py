"""Build sounds/ from sounds/sources.json: trimmed, loudness-matched Ogg Opus clips plus a manifest.

    python tools/build_soundbank.py            # build clips that are missing or whose source changed
    python tools/build_soundbank.py --force    # rebuild everything

sources.json entries: {"key", "path", "keywords", "mood", "description", optional "start" and "seconds"}.
ffmpeg runs single-threaded at below-normal priority so it stays out of the way of other work.
"""

from __future__ import annotations

import array
import base64
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOUNDS = ROOT / "sounds"
MAX_SECONDS = 7.0
WAVEFORM_POINTS = 64
DECODE_RATE = 8000


def find_ffmpeg() -> str:
    for candidate in (ROOT.parent / "ffmpeg.exe", ROOT / "ffmpeg.exe"):
        if candidate.is_file():
            return str(candidate)
    found = shutil.which("ffmpeg")
    if not found:
        sys.exit("ffmpeg not found: put ffmpeg.exe next to the project or on PATH.")
    return found


def run(ffmpeg: str, *args: str) -> bytes:
    flags = getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    result = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-threads", "1", *args],
        capture_output=True,
        creationflags=flags,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode(errors="replace").strip() or "ffmpeg failed")
    return result.stdout


def encode(ffmpeg: str, source: Path, target: Path, start: float, seconds: float) -> None:
    filters = ",".join(
        [
            "silenceremove=start_periods=1:start_threshold=-45dB",  # meme timing: no dead air up front
            f"atrim=0:{seconds}",
            "loudnorm=I=-16:TP=-1.5:LRA=11",
            "aresample=48000",
        ]
    )
    run(
        ffmpeg, "-y", "-ss", str(start), "-i", str(source), "-vn", "-ac", "1", "-af", filters,
        "-c:a", "libopus", "-b:a", "64k", "-application", "audio", str(target),
    )


def measure(ffmpeg: str, clip: Path) -> tuple[float, bytes]:
    """Duration and a 0–255 amplitude waveform, as Discord voice messages expect."""
    pcm = array.array("h", run(ffmpeg, "-i", str(clip), "-f", "s16le", "-ac", "1", "-ar", str(DECODE_RATE), "-"))
    seconds = len(pcm) / DECODE_RATE
    if not pcm:
        return 0.0, bytes(WAVEFORM_POINTS)
    size = max(1, math.ceil(len(pcm) / WAVEFORM_POINTS))
    levels = [
        math.sqrt(sum(sample * sample for sample in pcm[index:index + size]) / len(pcm[index:index + size]))
        for index in range(0, len(pcm), size)
    ]
    peak = max(levels) or 1.0
    return seconds, bytes(min(255, int(255 * level / peak)) for level in levels)


def main() -> None:
    force = "--force" in sys.argv
    sources = json.loads((SOUNDS / "sources.json").read_text(encoding="utf-8"))
    previous = {}
    manifest_path = SOUNDS / "sounds.json"
    if manifest_path.is_file():
        previous = {entry["key"]: entry for entry in json.loads(manifest_path.read_text(encoding="utf-8"))["sounds"]}
    ffmpeg = find_ffmpeg()

    built = []
    for source in sources:
        key = source["key"]
        clip = SOUNDS / f"{key}.ogg"
        origin = Path(source["path"])
        stamp = f"{origin}|{source.get('start', 0)}|{source.get('seconds', MAX_SECONDS)}"
        old = previous.get(key)
        try:
            if force or not clip.is_file() or not old or old.get("source") != stamp:
                if not origin.is_file():
                    print(f"  skip {key}: source missing ({origin})")
                    continue
                encode(ffmpeg, origin, clip, float(source.get("start", 0)), min(MAX_SECONDS, float(source.get("seconds", MAX_SECONDS))))
                print(f"  built {key}")
            seconds, waveform = measure(ffmpeg, clip)
        except RuntimeError as error:
            print(f"  fail {key}: {error}")
            continue
        built.append(
            {
                "key": key,
                "file": clip.name,
                "seconds": round(seconds, 2),
                "keywords": [keyword.casefold() for keyword in source.get("keywords", [])],
                "mood": source.get("mood", ""),
                "description": source.get("description", ""),
                "waveform": base64.b64encode(waveform).decode(),
                "source": stamp,
            }
        )
    manifest_path.write_text(json.dumps({"sounds": built}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{len(built)} sounds in {manifest_path}")


if __name__ == "__main__":
    main()
