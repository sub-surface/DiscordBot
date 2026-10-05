"""A soundbank of short meme clips the personas can play, sent as Discord voice messages.

Models choose a sound by writing a tag such as ``[sound: vine_boom]`` (the tag is removed from
the text). When a model doesn't, a keyword match on the conversation may pick one, at a
lower rate and with a per-channel cooldown so channels don't turn into a soundboard.

Clips are built by tools/build_soundbank.py into sounds/ (Ogg Opus plus a manifest with each
clip's duration and waveform). The audio files are git-ignored: they come from a local sample
library, so they stay on this machine.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

import discord
from discord.http import handle_message_parameters

log = logging.getLogger("psychograph.sounds")

TAG = re.compile(r"\[\s*(?:sound|sfx|sound ?effect)\s*[:=]\s*([^\]\n]{1,40})\]", re.IGNORECASE)
ACTION = re.compile(r"(?<![\w*])\*([a-z][a-z0-9 _'-]{1,30})\*(?![\w*])", re.IGNORECASE)
TAG_COOLDOWN_SECONDS = 15
KEYWORD_COOLDOWN_SECONDS = 120
KEYWORD_CHANCE = 0.35
VOICE_MESSAGE_FLAG = 8192


def normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")


@dataclass(frozen=True)
class Sound:
    key: str
    path: Path
    seconds: float
    keywords: tuple[str, ...]
    mood: str
    description: str
    waveform: bytes

    @property
    def label(self) -> str:
        return self.key.replace("_", " ")


class VoiceMessageFile(discord.File):
    """An Ogg Opus attachment carrying the duration and waveform Discord needs for a voice message."""

    def __init__(self, sound: Sound) -> None:
        super().__init__(sound.path, filename="voice-message.ogg")
        self.seconds = sound.seconds
        self.waveform = sound.waveform

    def to_dict(self, index: int) -> dict:
        payload = super().to_dict(index)
        payload["duration_secs"] = round(self.seconds, 2)
        payload["waveform"] = base64.b64encode(self.waveform).decode()
        return payload


class Soundbank:
    def __init__(self, directory: Path, rng: random.Random | None = None) -> None:
        self.directory = directory
        self.rng = rng or random.Random()
        self.sounds: dict[str, Sound] = {}
        self._last_played: dict[int, float] = {}
        self.load()

    def load(self) -> None:
        manifest = self.directory / "sounds.json"
        self.sounds = {}
        if not manifest.is_file():
            return
        for entry in json.loads(manifest.read_text(encoding="utf-8")).get("sounds", []):
            path = self.directory / entry["file"]
            if not path.is_file():
                continue  # not built on this machine
            self.sounds[entry["key"]] = Sound(
                key=entry["key"],
                path=path,
                seconds=float(entry["seconds"]),
                keywords=tuple(entry.get("keywords", ())),
                mood=entry.get("mood", ""),
                description=entry.get("description", ""),
                waveform=base64.b64decode(entry.get("waveform", "")),
            )
        log.info("Loaded %d sounds", len(self.sounds))

    def __len__(self) -> int:
        return len(self.sounds)

    def get(self, name: str) -> Sound | None:
        key = normalise(name)
        return self.sounds.get(key) or next(
            (sound for sound in self.sounds.values() if normalise(sound.label) == key), None
        )

    def search(self, query: str) -> list[Sound]:
        wanted = query.casefold().strip()
        return [
            sound
            for sound in self.sounds.values()
            if wanted in sound.label.casefold() or any(wanted in keyword for keyword in sound.keywords)
        ]

    def menu(self, compact: bool) -> str:
        """The instruction telling a model which sounds exist and how to use them."""
        if compact:
            names = ", ".join(self.sounds)
            return f"You may add one sound effect when it really fits, by ending with [sound: name]. Names: {names}."
        listing = "; ".join(f"{sound.key} ({sound.description or sound.mood})" for sound in self.sounds.values())
        return (
            "You can play one short sound effect with your reply by writing [sound: name] at the end. "
            "Use it sparingly, only when a sound would land as a joke or punctuation — most replies need none. "
            f"Available sounds: {listing}."
        )

    def extract(self, text: str) -> tuple[str, Sound | None]:
        """Remove sound tags from a reply; return the text and the first sound that exists."""
        chosen: Sound | None = None
        for match in TAG.finditer(text):
            chosen = chosen or self.get(match.group(1))
        text = TAG.sub("", text)
        if chosen is None:
            # Weaker models often narrate the sound instead: "*vine boom*".
            for match in ACTION.finditer(text):
                sound = self.get(match.group(1).removesuffix(" sound").removesuffix(" sfx"))
                if sound:
                    chosen = sound
                    break
        return re.sub(r"[ \t]+\n", "\n", text).strip(), chosen

    def by_keyword(self, *texts: str) -> Sound | None:
        """A sound whose keywords appear in the text, at random among matches."""
        haystack = " ".join(texts).casefold()
        matches = [
            sound
            for sound in self.sounds.values()
            if any(re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", haystack) for keyword in sound.keywords)
        ]
        return self.rng.choice(matches) if matches else None

    def choose(self, channel_id: int, tagged: Sound | None, *texts: str) -> Sound | None:
        """Apply cooldowns: a tagged sound needs a short gap; a keyword match is rarer and needs a long one."""
        since = time.monotonic() - self._last_played.get(channel_id, -1e9)
        if tagged is not None:
            sound = tagged if since >= TAG_COOLDOWN_SECONDS else None
        elif since >= KEYWORD_COOLDOWN_SECONDS and self.rng.random() < KEYWORD_CHANCE:
            sound = self.by_keyword(*texts)
        else:
            sound = None
        if sound is not None:
            self._last_played[channel_id] = time.monotonic()
        return sound


async def send_voice_message(
    channel: discord.abc.Messageable,
    sound: Sound,
    reference: discord.Message | None = None,
) -> discord.Message:
    """Post `sound` as a voice message (the waveform bubble). discord.py can't send these itself."""
    target = await channel._get_channel()
    state = target._state
    with handle_message_parameters(
        file=VoiceMessageFile(sound),
        flags=discord.MessageFlags._from_value(VOICE_MESSAGE_FLAG),
        allowed_mentions=discord.AllowedMentions.none(),
        message_reference=reference.to_message_reference_dict() if reference else discord.utils.MISSING,
    ) as params:
        data = await state.http.send_message(target.id, params=params)
    return state.create_message(channel=target, data=data)


async def play(
    channel: discord.abc.Messageable,
    sound: Sound,
    reference: discord.Message | None = None,
) -> discord.Message | None:
    """Send a voice message, falling back to a plain audio attachment if Discord refuses one."""
    try:
        return await send_voice_message(channel, sound, reference)
    except discord.HTTPException as error:
        log.info("Voice message refused (%s); sending %s as an attachment", error.status, sound.key)
    try:
        return await channel.send(
            file=discord.File(sound.path, filename=f"{sound.key}.ogg"),
            reference=reference,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        log.info("Couldn't send sound %s", sound.key)
        return None
