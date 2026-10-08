"""Jev, TypeSafe's System One model: fast, calibrated, typed decisions instead of generated text.

System 1 (Jev: ~0.4 s, about $0.00002 a call, no GPU) makes the narrow decisions; System 2 (the
language model on Modal or LM Studio) does the reasoning and writing. Here Jev:

  triages    every mention in one call: what it asks for (chat, a tool, a /quick feature, or a
             question about the bot answered from code) and which recent channel messages matter
             for the reply, so the model gets a short relevant transcript instead of all of it
  screens    a debate transcript before the review: is there an argument at all (if not, the
             review answers at once and the GPU is never woken), and which messages are fouls
  reads      the review's decision line back into a typed outcome for the leaderboard
  picks      the sound that fits an exchange, if any, instead of a keyword match
  notices    messages nobody sent the bot: one naming a persona that's spoken to it, and one
             good enough for the channel's persona to react to, and with which server emote (see ambient.py)

Every question and threshold lives in this file so they can be reviewed and tuned together.
Every call fails soft: on a missing key, an error or a timeout the caller carries on without it.
API: https://docs.typesafe.ai/api.md
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass

import aiohttp

from .conversation import Said, clip, speaker_label

log = logging.getLogger("psychograph.jev")

API_URL = "https://api.typesafe.ai/v1/systemone"
TIMEOUT_SECONDS = 4

ROUTE_CONFIDENCE = 0.85     # route away from chat only when this sure: a wrong route steals a chat reply
NEEDS_CONTEXT = 0.3         # below this, the message stands alone ("hewwo mochi") and gets almost no context
RELEVANT = 0.5              # a channel message this relevant to the reply joins the context
ALWAYS_KEEP = 6             # the last few channel messages are always kept: the conversation as it stands
QUIET_KEEP = 3              # …or just these, for a message that stands alone
FALLBACK_KEEP = 15          # without Jev, the last this many
MAX_KEPT = 25               # at most this many channel messages reach the model
TRIAGE_TEXT_CHARS = 300     # each message as Jev sees it
QUOTE_CHARS = 70            # a flagged message as quoted in the debate notes
SOUND_FITS = 0.75           # a sound must suit the moment at least this much (a noul)…
SOUND_CONFIDENCE = 0.2      # …and the pick be this confident: a ~50-way choice spreads confidence thin
NO_DEBATE = 0.15            # below this, the transcript has no argument worth a review
FOUL_CONFIDENCE = 0.6       # tag a message only when the foul is this confident (a choice confidence)
EVIDENCE_YES = 0.85         # call a message evidence-backed at this probability
MIN_WORDS = 4               # shorter messages ("lol", "41mm") aren't tagged
TAGGED_MESSAGES = 40        # tag at most the latest this many messages
SPLIT, NO_CONTEST = "split", "no_contest"
NAMED = 0.8                 # a message naming a persona is answered by it only when Jev is this sure it's spoken to
REACTS = 0.85               # the channel's persona reacts to a message it wasn't sent only when it lands this well…
EMOTE_CONFIDENCE = 0.35     # …and Jev is this sure which mood of emote fits
AMBIENT_MESSAGES = 6        # the conversation before a message, for both questions

CHAT = "Ordinary conversation or a request for the persona itself, not one of the other requests."
# Questions about the bot itself, answered from code. Kept narrow: "who are you?" to a persona is
# chat and gets an in-character answer; only questions about the bot's setup land here.
DIRECT = {
    "bot_help": "Asks what the bot can do, how to use it, or what its commands are.",
    "bot_persona": "Asks which persona the bot is currently set to, or which personas exist to switch to. "
    "About the bot's settings, not a question for the character about itself.",
    "bot_model": "Asks which AI model, backend, hardware or settings the bot is running on.",
}
# Whether a sound suits the moment is asked separately from which one: with "none" as one of ~50
# options it soaks up the probability and nothing ever plays, while a yes/no with explicit criteria
# separates jokes (~0.8–0.9) from sad or plain replies (~0.05–0.5).
SOUND_MOMENT = {
    "type": "noul",
    "instructions": "Would a meme sound effect after `reply` land as a joke or reaction, rather than feel out of place?",
    "criteria": {
        "true": "A playful, triumphant, dramatic, embarrassing or absurd moment, where a meme sound would be funny.",
        "false": "Serious, sad, sincere, informative or plain: a sound would feel out of place or tasteless.",
    },
}
SOUND_TEXT_CHARS = 600
FOULS = {
    "none": "A fair claim, question or reply, with no fallacy.",
    "ad_hominem": "Attacks the person, their motives or their group instead of their argument.",
    "strawman": "Misrepresents what the other person actually said, then argues against that.",
    "moving_goalposts": "After being answered, changes what they were claiming or what would count as proof.",
    "unsupported": "A sweeping or statistical factual claim with no evidence or reasoning given.",
    "whataboutism": "Deflects by pointing at a different issue instead of answering.",
}
FOUL_NAMES = {
    "ad_hominem": "ad hominem",
    "strawman": "strawman",
    "moving_goalposts": "moved the goalposts",
    "unsupported": "unsupported claim",
    "whataboutism": "whataboutism",
}


@dataclass(frozen=True)
class Flag:
    speaker: str
    quote: str
    label: str
    confidence: float


@dataclass(frozen=True)
class DebateNotes:
    debate: float                 # probability the transcript contains a real argument
    flags: tuple[Flag, ...]
    lean: str | None = None       # System 1's own pick of the stronger side; never shown to System 2
    lean_confidence: float = 0.0

    @property
    def no_debate(self) -> bool:
        return self.debate < NO_DEBATE

    def record(self) -> str:
        """The screening as kept with the review, for follow-ups that ask how the decision came about."""
        lines = [f'- **{flag.speaker}**: "{flag.quote}" → {flag.label} ({flag.confidence:.0%})' for flag in self.flags]
        if self.lean:
            lines.append(f"- System 1's own pick, not shown to the reviewer: {self.lean} ({self.lean_confidence:.0%})")
        return (
            "[Before this review was written, Jev (a fast classifier that gives probabilities, not reasons) screened "
            f"the transcript: argument likely ({self.debate:.0%}), and it tagged these messages:]\n" + "\n".join(lines)
        )

    def prompt_block(self) -> str:
        """Per-message tags for the reviewing model, framed as hints to check, not findings."""
        if not self.flags:
            return ""
        lines = [f'- **{flag.speaker}**: "{flag.quote}" → {flag.label} ({flag.confidence:.0%})' for flag in self.flags]
        return (
            "[System 1 notes: automatic per-message tags from a fast classifier. They may be wrong. "
            "Check each against the transcript before relying on it, and don't mention these notes.]\n"
            + "\n".join(lines)
        )


@dataclass(frozen=True)
class Triage:
    kind: str | None = None          # the route a message asks for; None means chat
    keep: tuple[int, ...] = ()       # indices of the channel messages that matter for the reply


def _labelled(messages: Sequence[Said]) -> list[tuple[str, Said]]:
    """Speakers as Jev sees them: members by name, the bot's own messages as [bot as name]."""
    return [(speaker_label(item), item) for item in messages]


class Jev:
    def __init__(self, api_key: str, model: str = "jev-latest") -> None:
        self.api_key = api_key.strip()
        self.model = model
        self._session: aiohttp.ClientSession | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)

    async def ask(self, state: object, questions: dict) -> dict | None:
        """The answers map, or None if Jev is off or the call failed."""
        if not self.enabled:
            return None
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS))
        payload = {"state": state, "model": self.model, "questions": questions}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            async with self._session.post(API_URL, json=payload, headers=headers) as response:
                if response.status != 200:
                    log.info("Jev returned %s: %s", response.status, (await response.text())[:300])
                    return None
                return (await response.json()).get("answers")
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as error:
            log.info("Jev call failed: %r", error)
            return None

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()

    # ── Decisions ───────────────────────────────────────────────────

    async def triage(self, text: str, speaker: str, messages: Sequence[Said], routes: dict[str, str]) -> Triage:
        """One call for a new message: which route it asks for (a key of `routes`, which maps each to a
        description of such requests; None means chat) and which of `messages` matter for the reply."""
        questions: dict = {}
        if routes and text.strip():
            # "chat" first: Jev leans slightly toward the first option, so the safe answer gets it.
            questions["kind"] = {
                "type": "choice",
                "instructions": "Which kind of request is `latest`?",
                "criteria": {"chat": CHAT, **routes},
            }
        if messages:
            questions["needs"] = {
                "type": "noul",
                "instructions": "Does replying well to `latest` depend on what was said earlier in `messages`?",
            }
            for index in range(len(messages)):
                questions[f"r{index}"] = {
                    "type": "noul",
                    "instructions": f"Does `messages[{index}]` help in understanding or replying to `latest`?",
                }
        if not questions:
            return Triage()
        state = {
            "messages": [{"speaker": label, "text": item.text[:TRIAGE_TEXT_CHARS]} for label, item in _labelled(messages)],
            "latest": {"speaker": speaker, "text": text[:TRIAGE_TEXT_CHARS]},
        }
        answers = await self.ask(state, questions) or {}
        kind = answers.get("kind")
        route = kind["choice"] if kind and kind["choice"] in routes and kind["confidence"] >= ROUTE_CONFIDENCE else None
        needs = answers.get("needs")
        if not messages:
            keep: tuple[int, ...] = ()
        elif needs is None:  # Jev unavailable
            keep = tuple(range(max(0, len(messages) - FALLBACK_KEEP), len(messages)))
        elif needs["noul"] < NEEDS_CONTEXT:
            keep = tuple(range(max(0, len(messages) - QUIET_KEEP), len(messages)))
        else:
            latest = set(range(max(0, len(messages) - ALWAYS_KEEP), len(messages)))
            relevant = {i for i in range(len(messages)) if answers.get(f"r{i}", {}).get("noul", 0) >= RELEVANT}
            keep = tuple(sorted(latest | relevant)[-MAX_KEPT:])
        return Triage(route, keep)

    async def pick_sound(self, request: str, reply: str, sounds: Sequence) -> str | None:
        """The key of the sound that fits this exchange, "none" when nothing should play (or Jev is unsure),
        and None if Jev couldn't be asked, so the caller can fall back to keywords."""
        if not sounds:
            return "none"
        catalogue = {sound.key: f"{sound.description or sound.label} ({sound.mood})" for sound in sounds}
        answers = await self.ask(
            {"message": request[:SOUND_TEXT_CHARS], "reply": reply[:SOUND_TEXT_CHARS]},
            {
                "moment": SOUND_MOMENT,
                "sound": {
                    "type": "choice",
                    "instructions": "Which sound effect best punctuates `reply` (an answer to `message`)?",
                    "criteria": catalogue,
                },
            },
        )
        moment, sound = (answers or {}).get("moment"), (answers or {}).get("sound")
        if not moment or not sound:
            return None
        fits = moment["noul"] >= SOUND_FITS and sound["confidence"] >= SOUND_CONFIDENCE
        return sound["choice"] if fits and sound["choice"] in catalogue else "none"

    async def addressed(self, text: str, speaker: str, name: str, before: Sequence[Said]) -> float:
        """How sure Jev is that `text`, which names the persona `name`, is said to it (0 if Jev can't be asked)."""
        answers = await self.ask(
            self._ambient_state(text, speaker, before, name=name),
            {"addressed": {
                "type": "noul",
                "instructions": "Is `latest` said to `name`, a character in this chat, so that `name` should answer?",
                "criteria": {
                    "true": "Speaks to `name`, asks `name` something, or teases, quotes or guesses at `name` "
                    "in a way `name` would naturally answer.",
                    "false": "Uses the word in another sense, or names `name` in passing to someone else, "
                    "where an answer from `name` would be an interruption.",
                },
            }},
        )
        return (answers or {}).get("addressed", {}).get("noul", 0.0)

    async def react(
        self, text: str, speaker: str, persona, before: Sequence[Said], moods: dict[str, str]
    ) -> tuple[float, str | None]:
        """Whether the persona, reading along, would react to `text`, and in which of `moods` (name → when it fits).
        (0, None) if Jev can't be asked or isn't sure of the mood."""
        voice = (persona.compact_prompt or persona.prompt)[:TRIAGE_TEXT_CHARS]
        answers = await self.ask(
            {**self._ambient_state(text, speaker, before, name=persona.name), "voice": voice},
            {
                "reacts": {
                    "type": "noul",
                    "instructions": "Would `name` (described by `voice`), reading along, react to `latest` with an emote?",
                    "criteria": {
                        "true": "It lands: funny, baffling, a roast, a puzzler worth chewing on, a dumb take, "
                        "or something `name` would have feelings about.",
                        "false": "Ordinary chat, a question to someone, or genuinely serious or private, where a "
                        "reaction from a bot would feel like noise.",
                    },
                },
                "mood": {"type": "choice", "instructions": "Which reaction fits `latest` best?", "criteria": moods},
            },
        )
        reacts, mood = (answers or {}).get("reacts"), (answers or {}).get("mood")
        if not reacts or not mood or mood["confidence"] < EMOTE_CONFIDENCE:
            return 0.0, None
        return reacts["noul"], mood["choice"]

    @staticmethod
    def _ambient_state(text: str, speaker: str, before: Sequence[Said], **extra: str) -> dict:
        return {
            "messages": [
                {"speaker": label, "text": item.text[:TRIAGE_TEXT_CHARS]} for label, item in _labelled(before[-AMBIENT_MESSAGES:])
            ],
            "latest": {"speaker": speaker, "text": text[:TRIAGE_TEXT_CHARS]},
            **extra,
        }

    async def debate_notes(self, said: Sequence[Said]) -> DebateNotes | None:
        """One call: is there an argument, which messages are fouls or evidence, and who System 1 thinks won."""
        if not said:
            return None
        messages = [{"speaker": label, "text": item.text} for label, item in _labelled(said)]
        speakers = sorted({item.speaker for item in said if item.author_id is not None})
        tagged = [
            index for index, item in enumerate(said)
            if item.author_id is not None and len(item.text.split()) >= MIN_WORDS
        ][-TAGGED_MESSAGES:]
        questions: dict = {
            "debate": {
                "type": "noul",
                "instructions": "In `messages`, do two or more people disagree and argue for opposing positions?",
            },
        }
        if len(speakers) >= 2:
            questions["lean"] = {
                "type": "choice",
                "instructions": "Who argued their position better in `messages`, judged on evidence, "
                "logic and answering the other side, not on volume or insults?",
                "criteria": {"even": "Nobody argued clearly better, or there was no argument.", **{name: None for name in speakers}},
            }
        for index in tagged:
            questions[f"foul_{index}"] = {
                "type": "choice",
                "instructions": f"As a move in the argument, which best describes `messages[{index}]`?",
                "criteria": FOULS,
            }
            questions[f"evidence_{index}"] = {
                "type": "noul",
                "instructions": f"Does `messages[{index}]` give specific evidence: data, a concrete example or a source?",
            }
        answers = await self.ask({"messages": messages}, questions)
        if not answers or "debate" not in answers:
            return None

        flags = []
        for index in tagged:
            foul, evidence = answers.get(f"foul_{index}"), answers.get(f"evidence_{index}")
            speaker, quote = said[index].speaker, clip(said[index].text, QUOTE_CHARS)
            if foul and foul["choice"] != "none" and foul["confidence"] >= FOUL_CONFIDENCE:
                flags.append(Flag(speaker, quote, FOUL_NAMES.get(foul["choice"], foul["choice"]), foul["confidence"]))
            elif evidence and evidence["noul"] >= EVIDENCE_YES:
                flags.append(Flag(speaker, quote, "gives evidence", evidence["noul"]))
        lean = answers.get("lean")
        return DebateNotes(
            debate=answers["debate"]["noul"],
            flags=tuple(flags),
            lean=lean["choice"] if lean and lean["choice"] != "even" else None,
            lean_confidence=lean["confidence"] if lean else 0.0,
        )

    async def read_verdict(self, decision: str, participants: Sequence[str]) -> str | None:
        """Who a review's decision line says won: a participant, SPLIT or NO_CONTEST (None if unsure)."""
        if not decision.strip() or not participants:
            return None
        criteria = {
            NO_CONTEST: "The decision says there was no real debate (no contest).",
            SPLIT: "The decision calls it even: a split decision or a draw.",
            **{name: f"The decision says {name} won." for name in participants},
        }
        answers = await self.ask(
            {"decision": decision},
            {"winner": {"type": "choice", "instructions": "What outcome does `decision` announce?", "criteria": criteria}},
        )
        answer = (answers or {}).get("winner")
        return answer["choice"] if answer and answer["confidence"] >= ROUTE_CONFIDENCE else None
