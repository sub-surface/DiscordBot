"""Halloween in the bot's channels (halloween.py has the calendar, the rates and the lines).

In season:   a ghost now and then reacts to a message and is gone ten seconds later; "trick or treat" (said plainly,
             or to the bot) gets a treat into your Scrollands pack, or a trick: spiders on your next few messages, a
             ghost that follows you, or the Pumpkin's kek.
The Night:   a Pandemonium demon wanders in from #nanospasm; an old line from the channel's own history flickers up
             and deletes itself; and everything above happens more often. (Possession, Death as the day's card,
             witnessed dice and the orange numogram live with chat, tarot, party and outside.)

No model anywhere.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from datetime import date, datetime, timedelta, timezone

import discord
from discord.ext import commands

from .. import halloween, icons, ledger
from .. import outside as xeno
from ..actions import act
from ..bot import PsychographBot, is_allowed_channel
from .outside import demon_card

log = logging.getLogger("psychograph.halloween")

NO_PINGS = discord.AllowedMentions.none()
VANISH_SECONDS = 10       # a haunting ghost, before it goes
FLICKER_SECONDS = 6       # an old line, before it deletes itself
FOLLOW_MESSAGES = 3       # how many of your messages a trick follows
OLD_LINE_AGE = timedelta(days=30)
_LINK = re.compile(r"https?://|<[@#&:a]")  # links, mentions, channels, emotes: not for flickering up


async def vanish(message: discord.Message, emoji, me: discord.abc.User, after: float | None = None) -> None:
    """React, then take it back: the ghost was only ever passing through."""
    try:
        await message.add_reaction(emoji)
        await asyncio.sleep(VANISH_SECONDS if after is None else after)
        await message.remove_reaction(emoji, me)
    except discord.HTTPException:
        pass


class HalloweenCog(commands.Cog):
    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot
        self.last: dict[tuple[str, int], float] = {}         # (what, channel) → when it last happened
        self.followed: dict[int, tuple[str, int]] = {}         # member → (trick, messages left)
        self.rng = random.SystemRandom()

    def _due(self, what: str, channel_id: int, rate: tuple[float, int]) -> bool:
        """A roll of `rate`'s chance, if the channel's cooldown for `what` is over; marks it when it passes."""
        chance, minutes = rate
        if time.time() - self.last.get((what, channel_id), 0) < minutes * 60 or self.rng.random() >= chance:
            return False
        self.last[(what, channel_id)] = time.time()
        return True

    def _spawn(self, coroutine) -> None:
        asyncio.get_running_loop().create_task(coroutine)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not halloween.season() or message.author.bot or message.webhook_id or self.bot.user is None:
            return
        if not is_allowed_channel(message.channel, self.bot.settings.allowed_channels):
            return
        if self.bot.ignoring(message.guild.id if message.guild else None, message.author.id) is not None:
            return
        if self._follow(message):
            return
        channel_id = message.channel.id
        if self._due("haunt", channel_id, halloween.haunt_rate()):
            self._spawn(vanish(message, icons.icon(self.bot, "ghost"), self.bot.user))
            act(".", message.channel, message.author, "haunted")
        elif halloween.night() and self._due("wander", channel_id, halloween.WANDER_RATE):
            await self._wander(message)
        elif halloween.night() and self._due("flicker", channel_id, halloween.GHOST_RATE):
            self._spawn(self._flicker(message.channel))

    def follow(self, user_id: int, trick: str) -> None:
        self.followed[user_id] = (trick, FOLLOW_MESSAGES)

    def _follow(self, message: discord.Message) -> bool:
        """A trick's spiders or ghost on the tricked member's next few messages."""
        trick, left = self.followed.get(message.author.id, ("", 0))
        if not left:
            return False
        if left > 1:
            self.followed[message.author.id] = (trick, left - 1)
        else:
            del self.followed[message.author.id]
        if trick == "spiders":
            self._spawn(self._react(message, icons.icon(self.bot, "spider")))
        else:
            self._spawn(vanish(message, icons.icon(self.bot, "ghost"), self.bot.user, after=4))
        return True

    @staticmethod
    async def _react(message: discord.Message, emoji) -> None:
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException:
            pass

    async def _wander(self, message: discord.Message) -> None:
        """The Night: a demon slips out of #nanospasm and is seen here, once."""
        demon = xeno.summon(rng=self.rng)
        view, file = demon_card(demon)
        view.children[0].add_item(discord.ui.TextDisplay("-# Nobody summoned it. It came out of #nanospasm on its own."))
        try:
            await message.channel.send(view=view, file=file, allowed_mentions=NO_PINGS)
        except discord.HTTPException:
            return
        act(".", message.channel, message.author, f"{demon.name} wandered in")

    async def _flicker(self, channel) -> None:
        """The Night: a line from the channel's own past posts itself again, with who said it and when, then goes."""
        before = datetime.now(timezone.utc) - OLD_LINE_AGE
        lines = []
        try:
            async for old in channel.history(limit=200, before=before):
                text = old.content.strip()
                if not old.author.bot and not old.webhook_id and 12 <= len(text) <= 200 and not _LINK.search(text):
                    lines.append(old)
        except (discord.HTTPException, AttributeError):
            return
        if not lines:
            return
        old = self.rng.choice(lines)
        text = discord.utils.escape_markdown(discord.utils.escape_mentions(old.content.strip())).replace("\n", " ")
        who = discord.utils.escape_markdown(getattr(old.author, "display_name", None) or old.author.name)
        try:
            sent = await channel.send(f"> {text}\n-# {who}, {old.created_at:%d %B %Y}", allowed_mentions=NO_PINGS)
            await asyncio.sleep(FLICKER_SECONDS)
            await sent.delete()
        except discord.HTTPException:
            pass


async def trick_or_treat(bot: PsychographBot, message: discord.Message, today: date | None = None) -> None:
    """Once a day each: a treat (candy, and keks, into the pack if they have a Scrollands character), or a trick."""
    guild_id = message.guild.id if message.guild else None
    user = message.author
    key = f"halloween:tot:{guild_id or 0}:{user.id}"
    today = (today or date.today()).isoformat()
    ghost = icons.icon(bot, "ghost")
    if bot.store.state(key) == today:
        await message.reply(f"{ghost} You already knocked today. The house is pretending nobody's home.", mention_author=False)
        return
    bot.store.set_state(key, today)
    character = bot.store.character(guild_id, user.id)
    outcome, what, keks = halloween.draw()
    if outcome == "treat":
        line = random.choice(halloween.TREAT_LINES)
        got = f"**{what}**" + (f" and **{keks} kek{'s' if keks != 1 else ''}**" if keks else "")
        if character is not None:
            entries = [ledger.gain(user.id, what, 1, "consumable", "trick or treat")]
            if keks:
                entries.append(ledger.keks(user.id, keks, "trick or treat"))
            ledger.commit(bot.store, guild_id, entries, "halloween")
            got += ", into your pack"
        else:
            got += " (make a character with `/character create` and the next one goes in your pack)" if keks else ""
        text = f"{ghost} {line}\n-# Treat: {got}."
    else:
        text = f"{icons.icon(bot, 'spider' if what == 'spiders' else 'ghost')} {halloween.TRICK_LINES[what]}"
        if what == "pumpkin":
            if character is not None and character.get("keks", 0) > 0:
                ledger.commit(bot.store, guild_id, [ledger.keks(user.id, -1, "the Pumpkin")], "halloween")
            else:
                text += "\n-# Your pockets were empty. The Pumpkin will remember."
        elif (cog := bot.get_cog("HalloweenCog")) is not None:
            cog.follow(user.id, what)
    await message.reply(text, mention_author=False, allowed_mentions=NO_PINGS)
    act("@", message.channel, user, f"trick or treat: {outcome} ({what})")


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(HalloweenCog(bot))
