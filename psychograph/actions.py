"""The action log: one short line per thing the bot did, in actions.log beside the bot (and the console).

    10-08 14:02:11 / #sim-city     Hugh Chungus   scores debates
    10-08 14:02:30 ^ #sim-city     Pearl          button "Right"
    10-08 14:02:41 @ #sim-city     Pearl          debate review 265t 6.0s warm
    10-08 14:02:41 + #sim-city     -              debate Pearl > Zack, plateau
    10-08 14:03:02 x #sim-city     Hugh Chungus   answer 1557464103604330527 (2 msgs)
    10-08 14:05:40 ! #shitpost     Zack           bot cost: not an admin

The sigil says what kind of action it was:
    /  a slash command or context menu, with its options    ^  a button, menu or form
    @  a reply sent (persona, tokens, seconds, cold/warm)    ~  a regenerate
    x  a deletion                                            +  a ledger entry: debate, prediction, duel
    -  a ledger entry skipped, and why                       !  refused or failed
    .  ambient: a reaction, sound, digest or slop report
grep a sigil column (`grep " ! " actions.log`) or a name to follow one thread.
"""

from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

import discord

log = logging.getLogger("psychograph.act")

WHERE, WHO = 13, 14  # column widths
CONTROL = re.compile(r"[\x00-\x1f\x7f\u0085\u2028\u2029]+")  # a name or option with a newline could forge a line


def setup(path: Path) -> None:
    """Also write the action log to `path`, rotated at 1 MB with three old files kept."""
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%m-%d %H:%M:%S"))
    log.addHandler(handler)


def _clip(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 1] + "…"


def where(channel: object) -> str:
    if channel is None or isinstance(channel, (discord.DMChannel, discord.GroupChannel)):
        return "dm"
    name = getattr(channel, "name", None)
    return f"#{name}" if name else f"#{getattr(channel, 'id', '?')}"


def who(user: object) -> str:
    if user is None:
        return "-"
    if isinstance(user, (str, int)):
        return str(user)
    return getattr(user, "display_name", None) or getattr(user, "name", None) or str(getattr(user, "id", "?"))


def act(sigil: str, channel: object, user: object, what: str) -> None:
    """One line: sigil, channel, member ("-" for the bot acting alone), then what happened."""
    name = who(user) if isinstance(user, int) else _clip(_flat(who(user)), WHO)  # a bare id (member not cached) stays whole
    log.info("%s %-*s %-*s %s", sigil, WHERE, _clip(_flat(where(channel)), WHERE), WHO, name, _flat(what))


def _flat(text: str) -> str:
    """One line, whatever members typed: line breaks and control characters become a space."""
    return CONTROL.sub(" ", text)


def options(data: list[dict] | None) -> str:
    """A command's options as `group sub key=value`, as typed."""
    parts = []
    for option in data or []:
        if option.get("type") in (1, 2):  # a subcommand or group
            parts.append(option["name"])
            parts.append(options(option.get("options")))
        elif "value" in option:
            value = str(option["value"])
            parts.append(f"{option['name']}={_clip(value, 40) if ' ' not in value else repr(_clip(value, 40))}")
    return " ".join(part for part in parts if part)


def interaction(item: discord.Interaction) -> None:
    """Every command, button, menu and form, as it arrives."""
    data = item.data or {}
    if item.type is discord.InteractionType.application_command:
        if data.get("type", 1) != 1:  # a context menu: what it was run on
            act("/", item.channel, item.user, f"{data.get('name')} on {data.get('target_id')}")
        else:
            act("/", item.channel, item.user, f"{data.get('name')} {options(data.get('options'))}".strip())
    elif item.type is discord.InteractionType.component:
        custom_id = str(data.get("custom_id", ""))
        label = _label(item.message, custom_id) or custom_id
        picked = f" = {', '.join(map(str, data['values']))}" if data.get("values") else ""
        act("^", item.channel, item.user, f"{'menu' if data.get('values') is not None else 'button'} {label!r}{picked}")
    elif item.type is discord.InteractionType.modal_submit:
        act("^", item.channel, item.user, "form submitted")


def _label(message: discord.Message | None, custom_id: str) -> str | None:
    """A component's label or placeholder (view custom ids are random hex)."""
    stack = list(getattr(message, "components", None) or [])
    while stack:
        component = stack.pop()
        stack.extend(getattr(component, "children", None) or [])
        if getattr(component, "custom_id", None) == custom_id:
            return getattr(component, "label", None) or getattr(component, "placeholder", None)
    return None


async def deny(item: discord.Interaction, text: str, why: str) -> None:
    """Refuse privately, and log it."""
    if item.command is not None:
        name = item.command.qualified_name
    else:
        custom_id = str((item.data or {}).get("custom_id", ""))
        name = repr(_label(item.message, custom_id) or "button")
    act("!", item.channel, item.user, f"{name}: {why}")
    await item.response.send_message(text, ephemeral=True)
