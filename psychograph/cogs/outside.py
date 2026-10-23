"""The Outside: what the bot does in #nanospasm (settings.outside_channels), and nothing else of it there.

Asked for:   "aq: Nick Land", "tx 2026", "summon" / "summon: <anything>" (a Pandemonium demon on the numogram, with its
             ASCII likeness), and /numogram aq | tx | summon (which also work in the bot's ordinary channels).
Ambient:     the channel's AQ index notices when two people's messages sum the same; a message (or a posted tweet)
             that a line in the quote bank already said gets the eye, and clicking the eye brings the line; now and
             then, while people are around, a fragment from the bank, with "Deeper" for the paragraph around it.
Not in /help (look here): a message whose AQ equals the bot's current name's gets the eye; something posted at 03:33 hears
             from the Warp; "the future" is sometimes received early; summoning the bot by name summons nothing.

No language model anywhere: arithmetic, a BM25 index and the fxtwitter API (free; one tweet per link, not timelines).
"""

from __future__ import annotations

import asyncio
import io
import logging
import random
import re
import time
from collections import deque
from datetime import datetime, timedelta, timezone

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import demon_art, icons
from .. import outside as xeno
from ..actions import act
from ..bot import PsychographBot, is_allowed_channel

log = logging.getLogger("psychograph.outside")

NO_PINGS = discord.AllowedMentions.none()
EYE = "noumenon"
QUOTE_ACCENT = 0x9E2A2B           # oxblood
INDEX_SIZE = 600                  # messages per channel the AQ index remembers
AQ_FLOOR = 150                    # below this, sums collide too easily to be worth remarking on
COINCIDENCE_EVERY = timedelta(hours=3)
SAID_EVERY = timedelta(minutes=40)  # per channel, between "already said this" eyes
FRAGMENT_EVERY = timedelta(hours=8)
FUTURE_EVERY = timedelta(days=1)
_TWEET = re.compile(r"https?://(?:www\.)?(?:x|twitter|fxtwitter|vxtwitter|fixupx|fixvx)\.com/(\w+)/status/(\d+)", re.IGNORECASE)


def _walk(components) -> list:
    """Every component in a message, nested ones included (Components V2 containers, sections, rows)."""
    out = []
    for component in components or []:
        out.append(component)
        out += _walk(getattr(component, "children", None) or getattr(component, "components", None))
        accessory = getattr(component, "accessory", None)
        if accessory is not None:
            out.append(accessory)
    return out


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rsplit(" ", 1)[0] + "…"


class Deeper(discord.ui.DynamicItem[discord.ui.Button], template=r"outside:deeper:(?P<id>[a-z]+-\d+)"):
    """The button under a fragment: the paragraph it came from, just for whoever asked. Survives restarts."""

    def __init__(self, quote_id: str) -> None:
        super().__init__(discord.ui.Button(label="Deeper", style=discord.ButtonStyle.secondary, custom_id=f"outside:deeper:{quote_id}"))
        self.quote_id = quote_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match) -> Deeper:
        return cls(match["id"])

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("OutsideCog")
        quote = cog.bank.by_id.get(self.quote_id) if cog else None
        if quote is None:
            await interaction.response.send_message("-# The page has gone back into the dark.", ephemeral=True)
            return
        paragraph = discord.utils.escape_mentions(_clip(quote.paragraph, 1800))
        await interaction.response.send_message(f"{paragraph}\n-# {quote.source}", ephemeral=True, allowed_mentions=NO_PINGS)


def quote_view(quote: xeno.Quote, heading: str = "", deeper: bool = True) -> discord.ui.LayoutView:
    children: list[discord.ui.Item] = []
    if heading:
        children.append(discord.ui.TextDisplay(heading))
    children.append(discord.ui.TextDisplay(f"> {discord.utils.escape_mentions(quote.text)}\n-# {quote.source}"))
    if deeper:
        children.append(discord.ui.ActionRow(Deeper(quote.id)))
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(*children, accent_colour=QUOTE_ACCENT))
    return view


def demon_card(demon: xeno.Demon, summoned_by: str = "", aq_value: int | None = None) -> tuple[discord.ui.LayoutView, discord.File]:
    """A summoning: who answered, where on the numogram (a picture), what it looks like (ASCII), and the arithmetic."""
    buffer = io.BytesIO()
    xeno.numogram(demon).save(buffer, format="PNG")
    file = discord.File(io.BytesIO(buffer.getvalue()), filename="numogram.png")
    high, low = demon.zones
    lore = [f"Zone {high}'s gate opens on {xeno.gate(high)}, zone {low}'s on {xeno.gate(low)}"]
    if demon.syzygy:
        lore.append(f"a syzygy: {high} and {low} make nine, and their current is {high - low}")
    if aq_value is not None:
        lore.append(f"called by AQ {aq_value} ({discord.utils.escape_mentions(summoned_by[:60])})")
    view = discord.ui.LayoutView(timeout=None)
    view.add_item(discord.ui.Container(
        discord.ui.Section(
            discord.ui.TextDisplay(f"### {demon.span} · {demon.name}\n*{demon.kind}* · {demon.power}"),
            accessory=discord.ui.Thumbnail("attachment://numogram.png", description=f"The numogram, {demon.span} lit"),
        ),
        discord.ui.TextDisplay(f"```\n{demon_art.avatar(demon)}\n```"),
        discord.ui.TextDisplay("-# " + " · ".join(lore) + " · Pandemonium Matrix, Ccru"),
        accent_colour=xeno.accent(demon),
    ))
    return view, file


class OutsideCog(commands.Cog):
    numogram = app_commands.Group(name="numogram", description="The Outside: AQ, Tic-Xenotation, and the demons of Pandemonium")

    def __init__(self, bot: PsychographBot) -> None:
        self.bot = bot
        self.bank = xeno.Bank.load()
        self.index: dict[int, deque] = {}              # channel → (aq, message id, author id, jump url)
        self.seeded: set[int] = set()
        self.eyed: dict[int, tuple[str, str]] = {}     # message id → (quote id, heading) awaiting a click on the eye
        self.last: dict[tuple[str, int], float] = {}   # (what, channel) → when it last happened
        self._session: aiohttp.ClientSession | None = None
        bot.add_dynamic_items(Deeper)

    async def cog_unload(self) -> None:
        self.fragments.cancel()
        if self._session is not None:
            await self._session.close()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if not self.fragments.is_running():
            self.fragments.start()

    # ── Helpers ──

    def _outside(self, channel) -> bool:
        return is_allowed_channel(channel, self.bot.settings.outside_channels)

    def _due(self, what: str, channel_id: int, every: timedelta) -> bool:
        return time.time() - self.last.get((what, channel_id), 0) >= every.total_seconds()

    def _mark(self, what: str, channel_id: int) -> None:
        self.last[(what, channel_id)] = time.time()

    async def _eye(self, message: discord.Message, quote: xeno.Quote, heading: str) -> None:
        try:
            await message.add_reaction(icons.icon(self.bot, EYE))
        except discord.HTTPException:
            return
        self.eyed[message.id] = (quote.id, heading)
        while len(self.eyed) > 200:
            self.eyed.pop(next(iter(self.eyed)))

    # ── Messages ──

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self._outside(message.channel) or self.bot.user is None or message.author.id == self.bot.user.id:
            return
        if message.author.bot or message.webhook_id:
            await self._tweets(message)  # MonitoRSS and other feeds: tweets are worth reading; nothing else is
            return
        if self.bot.ignoring(message.guild.id if message.guild else None, message.author.id) is not None:
            return
        if (asked := xeno.request(message.content)) is not None:
            await self._answer(message, *asked)
            return
        if _TWEET.search(message.content):
            await self._tweets(message)
        else:
            await self._already_said(message)
        await self._coincidence(message)
        await self._secrets(message)

    async def _answer(self, message: discord.Message, kind: str, text: str) -> None:
        if kind == "aq":
            reply = self.aq_line(text)
        elif kind == "tx":
            reply = self.tx_line(text)
        else:
            view, file = self.summoning(text, message.author, message.guild.id if message.guild else None)
            await message.reply(view=view, file=file, mention_author=False, allowed_mentions=NO_PINGS)
            act("@", message.channel, message.author, f"summoned {text[:30] or 'at random'}")
            return
        await message.reply(reply, mention_author=False, allowed_mentions=NO_PINGS)
        act("@", message.channel, message.author, f"{kind} {text[:30]}")

    def aq_line(self, text: str) -> str:
        value = xeno.aq(text)
        shown = discord.utils.escape_mentions(discord.utils.escape_markdown(_clip(text, 80)))
        demon = xeno.demons()[value % len(xeno.demons())]
        return (f"**AQ {value}** · {shown}\n-# folds to zone {xeno.reduce(value)} · "
                f"{demon.name} ({demon.span}) answers to it")

    @staticmethod
    def tx_line(number: str) -> str:
        try:
            value = int(number)
            return f"**{value:,}** in Tic-Xenotation\n`{xeno.tx(value)}`\n-# ':' is 2, (n) the nth prime · Barker, via Land's *Tic-Talk*"
        except ValueError as error:
            return f"-# {error}"

    def _names(self, guild_id: int | None) -> set[str]:
        """Everything the bot answers to: its nickname here, its own name, and what it was before."""
        names = {self.bot.name_in(guild_id), getattr(self.bot.user, "display_name", ""), "Mecha Epstein", "Mechirk Fucking Epstein"}
        return {name.casefold() for name in names if name}

    def summoning(self, text: str, who: discord.abc.User, guild_id: int | None = None) -> tuple[discord.ui.LayoutView, discord.File]:
        named = next((demon for demon in xeno.demons() if demon.name.casefold() == text.casefold().strip()), None)
        names = self._names(guild_id)
        if text and named is None and (text.casefold().strip() in names or xeno.aq(text) in {xeno.aq(name) for name in names}):
            view = discord.ui.LayoutView(timeout=None)  # a secret: the machine is not one of the 45
            view.add_item(discord.ui.Container(discord.ui.TextDisplay(
                "### 10::0 · ——\n*There is no forty-sixth demon.*\n```\n\n          ::\n        .-''-.\n       ( o  o )\n        '-..-'\n          ::\n\n```\n"
                "-# It was already here when you called. · Pandemonium Matrix, Ccru (unlisted)"), accent_colour=0x14161B))
            buffer = io.BytesIO()
            xeno.numogram(None).save(buffer, format="PNG")
            return view, discord.File(io.BytesIO(buffer.getvalue()), filename="numogram.png")
        if named is not None:
            return demon_card(named)
        demon = xeno.summon(text)
        return demon_card(demon, text, xeno.aq(text) if text else None)

    async def _already_said(self, message: discord.Message) -> None:
        if len(message.content.split()) < 6 or not self._due("said", message.channel.id, SAID_EVERY):
            return
        found = self.bank.match(message.content)
        if found is None:
            return
        self._mark("said", message.channel.id)
        await self._eye(message, found[0], "-# Already said:")
        act(".", message.channel, message.author, f"already said ({found[1]:.0f})")

    async def _tweets(self, message: discord.Message) -> None:
        """A tweet in the channel (a link, or a feed's card): its full text from fxtwitter, matched against the bank."""
        urls = [message.content] + [getattr(item, "url", "") or "" for item in _walk(message.components)]
        found = next((match for url in urls for match in [_TWEET.search(url)] if match), None)
        if found is None:
            return
        text = await self._tweet_text(found.group(2))
        if not text:
            text = " ".join(getattr(item, "content", "") or "" for item in _walk(message.components)) or message.content
        match = self.bank.match(text)
        if match is not None:
            year = match[0].year
            await self._eye(message, match[0], f"-# @{found.group(1)} now; {match[0].author.split()[-1]}, {year}:")
            act(".", message.channel, "-", f"tweet matched ({match[1]:.0f})")

    async def _tweet_text(self, status_id: str) -> str:
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8),
                                                  headers={"User-Agent": "PsychographDiscordBot/1.0"})
        try:
            async with self._session.get(f"https://api.fxtwitter.com/status/{status_id}") as response:
                if response.status != 200:
                    return ""
                data = await response.json()
            return ((data or {}).get("tweet") or {}).get("text") or ""
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return ""

    async def _coincidence(self, message: discord.Message) -> None:
        """Two people, the same AQ: noted once in a while, quietly, with a link to the other message."""
        channel_id = message.channel.id
        index = self.index.setdefault(channel_id, deque(maxlen=INDEX_SIZE))
        if channel_id not in self.seeded:
            self.seeded.add(channel_id)
            try:
                async for old in message.channel.history(limit=INDEX_SIZE, before=message):
                    if not old.author.bot and old.content:
                        index.appendleft((xeno.aq(old.content), old.id, old.author.id, old.jump_url))
            except discord.HTTPException:
                pass
        value = xeno.aq(message.content)
        words = len(message.content.split())
        twin = next((entry for entry in reversed(index) if entry[0] == value and entry[2] != message.author.id), None)
        index.append((value, message.id, message.author.id, message.jump_url))
        if twin is None or value < AQ_FLOOR or not 3 <= words <= 40 or not self._due("aq", channel_id, COINCIDENCE_EVERY):
            return
        if random.random() > 0.5:
            return
        self._mark("aq", channel_id)
        await message.reply(f"-# AQ {value} ≡ {twin[3]}", mention_author=False, allowed_mentions=NO_PINGS, suppress_embeds=True)
        act(".", message.channel, message.author, f"AQ coincidence {value}")

    async def _secrets(self, message: discord.Message) -> None:
        bot_name = self.bot.name_in(message.guild.id if message.guild else None)
        if bot_name and len(message.content) > 3 and xeno.aq(message.content) == xeno.aq(bot_name):
            try:
                await message.add_reaction(icons.icon(self.bot, EYE))  # it heard its name
            except discord.HTTPException:
                pass
        local = message.created_at.astimezone()
        if (local.hour, local.minute) == (3, 33) and self._due("warp", message.channel.id, timedelta(hours=20)):
            self._mark("warp", message.channel.id)
            await message.reply("-# 6::3 · Djynxx · Time-Lapse", mention_author=False, allowed_mentions=NO_PINGS)
        if re.search(r"\bthe future\b", message.content, re.IGNORECASE) and self._due("future", message.channel.id, FUTURE_EVERY) \
                and random.random() < 0.15:
            self._mark("future", message.channel.id)
            arrives = datetime.now(timezone.utc) + timedelta(days=random.randint(400, 365 * 40))
            await message.reply(f"-# Received early. Sent <t:{int(arrives.timestamp())}:R>.", mention_author=False, allowed_mentions=NO_PINGS)

    # ── The eye: clicking it brings what was already said ──

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.message_id not in self.eyed or self.bot.user is None or payload.user_id == self.bot.user.id:
            return
        if payload.member is not None and payload.member.bot:
            return
        if payload.emoji.name not in (EYE, icons.ICONS[EYE][1].rstrip("️"), icons.ICONS[EYE][1]):
            return
        quote_id, heading = self.eyed.pop(payload.message_id)
        quote = self.bank.by_id.get(quote_id)
        channel = self.bot.get_channel(payload.channel_id)
        if quote is None or channel is None:
            return
        reference = discord.MessageReference(message_id=payload.message_id, channel_id=payload.channel_id, fail_if_not_exists=False)
        await channel.send(view=quote_view(quote, heading), reference=reference, mention_author=False, allowed_mentions=NO_PINGS)
        act(".", channel, "-", "already said: revealed")

    # ── Fragments, now and then ──

    @tasks.loop(minutes=30)
    async def fragments(self) -> None:
        if not self.bank.quotes:
            return
        for guild in self.bot.guilds:
            for channel in guild.text_channels:
                if not self._outside(channel) or not self._due("fragment", channel.id, FRAGMENT_EVERY) or random.random() > 0.3:
                    continue
                try:
                    recent = [message async for message in channel.history(limit=15)]
                except discord.HTTPException:
                    continue
                since = datetime.now(timezone.utc) - timedelta(hours=3)
                if not any(not message.author.bot and message.created_at > since for message in recent):
                    continue  # nobody around: a fragment into silence is litter
                self._mark("fragment", channel.id)
                quote = self.bank.random()
                await channel.send(view=quote_view(quote), allowed_mentions=NO_PINGS)
                act(".", channel, "-", f"fragment {quote.id}")

    # ── /numogram ──

    @numogram.command(name="summon", description="Call a demon of Pandemonium: at random, or by the AQ of what you write")
    @app_commands.describe(by="A name, a question, anything: its AQ chooses the demon (empty: chance does)")
    async def summon_command(self, interaction: discord.Interaction, by: str = "") -> None:
        view, file = self.summoning(by, interaction.user, interaction.guild_id)
        await interaction.response.send_message(view=view, file=file, allowed_mentions=NO_PINGS)
        act("^", interaction.channel, interaction.user, f"summoned {by[:30] or 'at random'}")

    @numogram.command(name="aq", description="Anglossic Qabbala: 0-9 at face value, A=10 … Z=35, summed")
    async def aq_command(self, interaction: discord.Interaction, text: str) -> None:
        await interaction.response.send_message(self.aq_line(text), allowed_mentions=NO_PINGS)

    @numogram.command(name="tx", description="A number in Tic-Xenotation, Land and Barker's prime-factor numerals")
    async def tx_command(self, interaction: discord.Interaction, number: app_commands.Range[int, 0, xeno.TX_LIMIT - 1]) -> None:
        await interaction.response.send_message(self.tx_line(str(number)), allowed_mentions=NO_PINGS)


async def setup(bot: PsychographBot) -> None:
    await bot.add_cog(OutsideCog(bot))
