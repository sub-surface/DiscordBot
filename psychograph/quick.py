"""Quick answers: Jev alone, no language model, no GPU, about half a second.

Each feature asks Jev typed questions and fills a fixed card from the answers (choices with
probabilities, tiers, bars). Jev can't write text, so the cards are the reply. Reached by
`/quick …`, by starting a mention with a trigger ("@bot decide: pizza or curry"), or by Jev
routing a plain-language mention here. All the questions live in this file; thresholds in jev.py.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field

import discord

from .conversation import Said, clip, speaker_label
from .jev import Jev
from .personas import Persona
from .render import EMBED_COLOR

INSTANT_FOOTER = "Instant · Jev · no model call"
CHAT_MESSAGES = 15        # recent messages a decision or the odds take into account
VIBE_MESSAGES = 40
CHATTER_MESSAGES = 15
MAX_ITEMS = 12
TEXT_CHARS = 300


@dataclass
class QuickRequest:
    text: str                                   # what was asked, without the trigger
    speaker: str                                # who asked
    author_id: int | None = None
    said: Sequence[Said] = ()                   # recent channel messages, oldest first
    target: str = ""                            # the message being rated or toned, if not the text
    chatters: Sequence[Persona] = field(default_factory=list)


@dataclass(frozen=True)
class Quick:
    key: str
    usage: str                                  # an example, for /help
    triggers: tuple[str, ...]
    intent: str                                 # what a plain-language request for it looks like; "" = never routed
    run: Callable[[Jev, QuickRequest], Awaitable[discord.Embed]]


# ── Card helpers ────────────────────────────────────────────────────

def bar(share: float, width: int = 12) -> str:
    filled = round(max(0.0, min(1.0, share)) * width)
    return "█" * filled + "░" * (width - filled)


def card(title: str, description: str = "") -> discord.Embed:
    embed = discord.Embed(title=title, description=description or None, color=EMBED_COLOR)
    embed.set_footer(text=INSTANT_FOOTER)
    return embed


def unavailable() -> discord.Embed:
    return card("Couldn't ask Jev", "Quick answers need Jev, which didn't answer. Try again in a moment.")


def _chat(said: Sequence[Said], count: int) -> list[dict]:
    return [{"speaker": speaker_label(item), "text": clip(item.text, TEXT_CHARS)} for item in said[-count:]]


def split_items(text: str, separators: str = r",|\n|;") -> list[str]:
    items, seen = [], set()
    for item in re.split(separators, text):
        item = item.strip(" \t.?!:-*•")
        if item and item.casefold() not in seen:
            seen.add(item.casefold())
            items.append(item)
    return items[:MAX_ITEMS]


LEAD_IN = re.compile(
    r"^\s*(?:(?:should|shall|do|can) (?:i|we)(?: (?:get|have|do|go for|go|watch|play|eat|pick))?"
    r"|which (?:is better|one)|what(?:'s| is) better|pick|choose|between)\b[\s:,]*",
    re.IGNORECASE,
)


def split_options(text: str) -> list[str]:
    """ "should we get pizza or curry or a shake?" → ["pizza", "curry", "a shake"] """
    return split_items(LEAD_IN.sub("", text), r",|\n|;|/|\s+or\s+|\s+vs\.?\s+")


# ── Features ────────────────────────────────────────────────────────

async def decide(jev: Jev, request: QuickRequest) -> discord.Embed:
    options = split_options(request.text)
    if len(options) < 2:
        return card("Decide", "Give me at least two options, like `decide: pizza or curry`.")
    answers = await jev.ask(
        {"question": clip(request.text, TEXT_CHARS), "chat": _chat(request.said, CHAT_MESSAGES)},
        {"pick": {
            "type": "choice",
            "instructions": "Given `chat`, which option best answers `question` for these people?",
            "criteria": {option: None for option in options},
        }},
    )
    pick = (answers or {}).get("pick")
    if not pick:
        return unavailable()
    rows = sorted(pick["probabilities"].items(), key=lambda row: -row[1])
    return card(f"Decision: {pick['choice']}", "\n".join(f"`{bar(p)}` {p:>4.0%}  {option}" for option, p in rows))


ODDS_BANDS = ((0.9, "Almost certainly"), (0.7, "Likely"), (0.4, "Coin flip"), (0.15, "Unlikely"), (0.0, "Almost certainly not"))


async def odds(jev: Jev, request: QuickRequest) -> discord.Embed:
    question = request.text.strip()
    if not question:
        return card("Odds", "Ask a yes/no question, like `odds: zack mentions his shin today`.")
    answers = await jev.ask(
        {"question": clip(question, TEXT_CHARS), "chat": _chat(request.said, CHAT_MESSAGES)},
        {"yes": {"type": "noul", "instructions": "What is the probability that the answer to `question` is yes?"}},
    )
    yes = (answers or {}).get("yes")
    if not yes:
        return unavailable()
    p = yes["noul"]
    band = next(label for floor, label in ODDS_BANDS if p >= floor)
    return card(f"{band}: {p:.0%}", f"> {clip(question, 200)}\n`{bar(p)}`\n-# Jev's read of the question, not a forecast.")


TIERS = {
    "S": "Elite: as good as it gets, near-universally loved.",
    "A": "Great: clearly better than most.",
    "B": "Good: solid, above average.",
    "C": "Fine: average, forgettable.",
    "D": "Bad: below average, mostly disliked.",
    "F": "Terrible: widely hated or embarrassing.",
}


async def tier(jev: Jev, request: QuickRequest) -> discord.Embed:
    items = split_items(request.text)
    if len(items) < 2:
        return card("Tier list", "List at least two things, separated by commas: `tier list: crocs, tea, jury duty`.")
    answers = await jev.ask(
        {"items": items},
        {f"t{i}": {
            "type": "choice",
            "instructions": f"Which tier does `items[{i}]` belong in, by general opinion?",
            "criteria": TIERS,
        } for i in range(len(items))},
    )
    if not answers:
        return unavailable()
    placed: dict[str, list[str]] = {}
    for i, item in enumerate(items):
        placed.setdefault(answers.get(f"t{i}", {}).get("choice", "C"), []).append(item)
    lines = [f"**{name}**  {', '.join(placed[name])}" for name in TIERS if name in placed]
    return card("Tier list", "\n".join(lines))


DIVIDED = ["Almost everyone agrees", "Most agree", "Opinion is split", "Most would disagree", "Almost everyone would disagree"]


async def rate(jev: Jev, request: QuickRequest) -> discord.Embed:
    take = (request.target or request.text).strip()
    if not take:
        return card("Take rating", "Give me a take, or reply to one: `rate my take: cereal is a soup`.")
    answers = await jev.ask(
        {"take": clip(take, TEXT_CHARS)},
        {
            "divided": {"type": "score", "instructions": "How divided would people be over `take`?", "criteria": DIVIDED},
            "bit": {"type": "noul", "instructions": "Is `take` said as a joke or a bit rather than sincerely?"},
            "defensible": {"type": "noul", "instructions": "Could a reasonable person defend `take` with a decent argument?"},
        },
    )
    if not answers or not {"divided", "bit", "defensible"} <= answers.keys():
        return unavailable()
    divided, bit, defensible = answers["divided"]["score"], answers["bit"]["noul"], answers["defensible"]["noul"]
    lines = [
        f"> {clip(take, 200)}",
        f"`{bar(divided / 4)}` Heat: {DIVIDED[round(divided)].lower()}",
        f"`{bar(defensible)}` Defensible: {defensible:.0%}",
        f"`{bar(bit)}` {'A bit' if bit >= 0.5 else 'Sincere'} ({bit:.0%} bit)",
    ]
    return card("Take rating", "\n".join(lines))


TONES = {
    "sincere": "Sincere and serious, meant literally.",
    "sarcastic": "Sarcastic: means the opposite of what it says.",
    "joking": "A joke, not meant seriously.",
    "half joking": "Half joking: a joke with a real point behind it.",
    "affectionate": "Warm teasing or affection.",
    "hostile": "Genuinely hostile or angry.",
}


async def tone(jev: Jev, request: QuickRequest) -> discord.Embed:
    text = (request.target or request.text).strip()
    if not text:
        return card("Tone check", "Reply to a message with `tone check`, or right-click it → Apps → Tone check.")
    answers = await jev.ask(
        {"message": clip(text, TEXT_CHARS), "chat": _chat(request.said, 8)},
        {"tone": {"type": "choice", "instructions": "Given `chat`, what is the tone of `message`?", "criteria": TONES}},
    )
    found = (answers or {}).get("tone")
    if not found:
        return unavailable()
    rows = sorted(found["probabilities"].items(), key=lambda row: -row[1])[:3]
    lines = [f"> {clip(text, 200)}", *(f"`{bar(p)}` {p:>4.0%}  {name}" for name, p in rows)]
    return card(f"Tone: {found['choice']}", "\n".join(lines))


async def vibe(jev: Jev, request: QuickRequest) -> discord.Embed:
    said = list(request.said[-VIBE_MESSAGES:])
    members = sorted({item.speaker for item in said if not item.by_bot})
    if len(said) < 5:
        return card("Vibe check", "Not enough chat to read yet.")
    questions = {
        "heat": {"type": "score", "instructions": "How heated is the conversation in `messages`?", "criteria": ["Chill", "Lively", "Heated", "Hostile"]},
        "chaos": {"type": "score", "instructions": "How chaotic or all-over-the-place is `messages`?", "criteria": ["Focused", "Wandering", "Chaotic"]},
        "fun": {"type": "noul", "instructions": "Are people in `messages` mostly having fun?"},
    }
    if len(members) >= 2:
        questions["main"] = {
            "type": "choice",
            "instructions": "Who is the main character of `messages`: the person the conversation revolves around?",
            "criteria": {name: None for name in members},
        }
    answers = await jev.ask({"messages": _chat(said, VIBE_MESSAGES)}, questions)
    if not answers or not {"heat", "chaos", "fun"} <= answers.keys():
        return unavailable()
    heat, chaos, fun = answers["heat"]["score"], answers["chaos"]["score"], answers["fun"]["noul"]
    lines = [
        f"`{bar(heat / 3)}` Heat: {['chill', 'lively', 'heated', 'hostile'][round(heat)]}",
        f"`{bar(chaos / 2)}` Chaos: {['focused', 'wandering', 'chaotic'][round(chaos)]}",
        f"`{bar(fun)}` Fun: {fun:.0%}",
    ]
    if "main" in answers:
        lines.append(f"Main character: **{answers['main']['choice']}**")
    return card(f"Vibe check · last {len(said)} messages", "\n".join(lines))


async def chatter(jev: Jev, request: QuickRequest) -> discord.Embed:
    mine = [clip(item.text, TEXT_CHARS) for item in request.said if item.author_id is not None and item.author_id == request.author_id]
    mine = mine[-CHATTER_MESSAGES:]
    if len(mine) < 3 or len(request.chatters) < 2:
        return card("Which chatter are you?", "Say a few more things first: I need at least three recent messages.")
    voices = {persona.name: clip(persona.compact_prompt or persona.prompt, 400) for persona in request.chatters}
    answers = await jev.ask(
        {"messages": mine, "voices": voices},
        {"who": {
            "type": "choice",
            "instructions": "Whose voice in `voices` do `messages` sound most like?",
            "criteria": {name: None for name in voices},
        }},
    )
    who = (answers or {}).get("who")
    if not who:
        return unavailable()
    rows = sorted(who["probabilities"].items(), key=lambda row: -row[1])[:3]
    return card(
        f"{request.speaker} is {rows[0][1]:.0%} {who['choice']}",
        "\n".join(f"`{bar(p)}` {p:>4.0%}  {name}" for name, p in rows),
    )


QUICKS: dict[str, Quick] = {
    quick.key: quick
    for quick in (
        Quick("decide", "decide: pizza or curry", ("decide",),
              "Asks the bot to make a decision for them between two or more named options (X or Y?). "
              "Not a question about the bot's own tastes or opinions (do you like X or Y).", decide),
        Quick("odds", "odds: zack mentions his shin today", ("odds", "what are the odds"),
              "Asks how likely something is, or for the odds or chances of something happening.", odds),
        Quick("tier", "tier list: crocs, tea, jury duty", ("tier list",),
              "Asks for a tier list or ranking of several listed things.", tier),
        Quick("rate", "rate my take: cereal is a soup", ("rate my take", "rate this take"), "", rate),
        Quick("tone", "tone check (as a reply)", ("tone check",),
              "Asks whether a message was sarcastic, serious or joking.", tone),
        Quick("vibe", "vibe check", ("vibe check",), "Asks for a vibe check of the chat or how the chat is feeling.", vibe),
        Quick("chatter", "which chatter am I", ("which chatter",),
              "Asks which chatter or server member they are most like.", chatter),
    )
}


# What Jev was shown for each feature, so a persona asked "why?" later knows what the answer rested on.
INPUTS = {
    "decide": f"the question, its options and the last {CHAT_MESSAGES} channel messages",
    "odds": f"the question and the last {CHAT_MESSAGES} channel messages",
    "tier": "the list of items only, judged by general opinion",
    "rate": "the take only",
    "tone": "the message and the 8 messages before it",
    "vibe": f"the last {VIBE_MESSAGES} channel messages",
    "chatter": f"the person's last {CHATTER_MESSAGES} messages and each chatter persona's description",
}


def record(feature: Quick, request: QuickRequest, embed: discord.Embed) -> str:
    """What a persona sees of a quick answer when someone replies to it: what Jev was shown and what it said.
    Jev returns probabilities, not reasons, so the note says any "why" is the persona's reading."""
    body = re.sub(r"`[█░]*`\s*", "", embed.description or "").strip()
    asked = (request.target or request.text).strip()
    return "\n".join(filter(None, [
        f"[Instant answer from Jev ({feature.key}). Jev is a fast classifier: it scores fixed options and returns "
        f"probabilities, not reasons. It was shown {INPUTS.get(feature.key, 'the request')}. If asked why, reason "
        "from those inputs and say it's your reading of Jev, not Jev's own explanation.]",
        f"Asked: {clip(asked, 300)}" if asked else "",
        f"Answer: {embed.title}",
        body,
    ]))


def quick_for(text: str) -> tuple[Quick, str] | None:
    """The quick feature a message calls by trigger, and the rest of the message."""
    lowered = text.strip().casefold()
    calls = sorted(((trigger, quick) for quick in QUICKS.values() for trigger in quick.triggers), key=lambda c: -len(c[0]))
    for trigger, quick in calls:
        if re.match(rf"{re.escape(trigger)}(?!\w)", lowered):
            return quick, text.strip()[len(trigger):].lstrip(" :,-")
    return None


def routes() -> dict[str, str]:
    """Quick features Jev may route a plain-language mention to."""
    return {quick.key: quick.intent for quick in QUICKS.values() if quick.intent}
