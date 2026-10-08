"""Persona duels: two personas argue a topic over a few rounds, then Jev scores it.

Each turn is one model call with the duel so far as context; the verdict is one Jev call (who argued
better, and the line of the duel), so judging costs no GPU. Results feed /scores duels.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .jev import Jev
from .personas import Persona

MAX_ROUNDS = 3
TURN_CHARS = 600           # each turn as the opponent and Jev see it
DRAW_CONFIDENCE = 0.25     # a pick less sure than this is a draw

RULES = (
    "You are in a public debate, in front of the whole channel, against {opponent} on: {topic}. Stay fully in "
    "character and argue the way you really would. Two to four sentences. In rounds after the first, answer "
    "{opponent}'s last point directly before making your own. No greeting, no name label, no summary of the debate."
)


@dataclass(frozen=True)
class Turn:
    persona: Persona
    text: str


@dataclass(frozen=True)
class Verdict:
    winner: Persona | None                    # None: a draw, or Jev couldn't judge
    shares: dict[str, float]                  # persona name (or "even") → probability
    best: Turn | None = None                  # the line of the duel


def instruction(speaker: Persona, opponent: Persona, topic: str, turns: Sequence[Turn], round_no: int, rounds: int) -> tuple[str, str]:
    """(context, instruction) for one turn."""
    so_far = "\n".join(f"{turn.persona.name}: {turn.text[:TURN_CHARS]}" for turn in turns)
    context = RULES.format(opponent=opponent.name, topic=topic) + (f"\n\nThe debate so far:\n{so_far}" if so_far else "")
    opening = "You open the debate." if not turns else ""
    return context, f"Round {round_no} of {rounds}. {opening} Your turn, {speaker.name}.".replace("  ", " ")


async def judge(jev: Jev, topic: str, a: Persona, b: Persona, turns: Sequence[Turn]) -> Verdict:
    debate = [{"speaker": turn.persona.name, "text": turn.text[:TURN_CHARS]} for turn in turns]
    answers = await jev.ask(
        {"topic": topic, "debate": debate},
        {
            "winner": {
                "type": "choice",
                "instructions": "Who argued better in `debate` on `topic`: stronger points, better answers to the "
                "other side, and wit? Ignore who spoke last.",
                "criteria": {"even": "Neither argued clearly better.", a.name: None, b.name: None},
            },
            "best": {
                "type": "choice",
                "instructions": "Which is the best line of `debate`: the funniest or sharpest?",
                "criteria": {f"t{i}": turn.text[:200] for i, turn in enumerate(turns)},
            },
        },
    )
    if not answers or "winner" not in answers:
        return Verdict(None, {})
    pick = answers["winner"]
    winner = {a.name: a, b.name: b}.get(pick["choice"]) if pick["confidence"] >= DRAW_CONFIDENCE else None
    best = answers.get("best")
    index = int(best["choice"][1:]) if best and best["choice"][1:].isdigit() else None
    return Verdict(winner, dict(pick["probabilities"]), turns[index] if index is not None and index < len(turns) else None)
