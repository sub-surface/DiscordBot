import asyncio
import json
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import discord

from psychograph import slop
from psychograph.conversation import TWEET_LINK_RE

from .helpers import make_bot


def posted(message_id: int, text: str, reacts: int = 0, bot: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=message_id,
        content=text,
        author=SimpleNamespace(id=7, bot=bot, display_name="Santi"),
        webhook_id=None,
        reactions=[SimpleNamespace(count=reacts)] if reacts else [],
        created_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )


class FakeHistoryChannel:
    def __init__(self, messages: list) -> None:
        self.id, self.name, self.messages = 5, "sim-city", messages
        self.guild = SimpleNamespace(id=10)

    def history(self, limit=None, after=None, oldest_first=True):
        async def walk():
            for message in self.messages:
                if after is None or message.id > after.id:
                    yield message
        return walk()


class SlopTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = make_bot()

    def tearDown(self) -> None:
        self.bot.store.close()

    def test_every_mirror_counts_as_a_post_link(self) -> None:
        for host in ("x.com", "twitter.com", "fxtwitter.com", "fixupx.com", "vxtwitter.com", "mobile.x.com"):
            self.assertEqual(TWEET_LINK_RE.search(f"look https://{host}/someone/status/123?s=20").group(1), "123")

    def test_collect_keeps_member_shares_and_refreshes_reactions_on_rescan(self) -> None:
        base = discord.utils.time_snowflake(datetime.now(timezone.utc) - timedelta(hours=1))
        channel = FakeHistoryChannel([
            posted(base + 1, "https://fixupx.com/a/status/111 and again https://x.com/a/status/111"),
            posted(base + 2, "bot post https://x.com/b/status/222", bot=True),
            posted(base + 3, "no links here"),
        ])
        asyncio.run(slop.collect(self.bot, channel))
        self.assertEqual([s["status_id"] for s in self.bot.store.shares(5)], ["111"])

        channel.messages[0].reactions = [SimpleNamespace(count=4)]
        asyncio.run(slop.collect(self.bot, channel))  # within the rescan window: updated, not duplicated
        self.assertEqual([(s["status_id"], s["reacts"]) for s in self.bot.store.shares(5)], [("111", 4)])

    def test_ranking_blends_reach_with_reactions_here(self) -> None:
        now = time.time()
        for message_id, status_id, reacts in ((1, "big", 0), (2, "loved", 6), (3, "dud", 0)):
            self.bot.store.save_share(message_id, status_id, 10, 5, 7, "Santi", now, reacts)
        stats = {
            "big": {"likes": 50_000, "reposts": 9_000, "views": 4_000_000},
            "loved": {"likes": 900, "reposts": 40, "views": 60_000},
            "dud": {"likes": 3, "reposts": 0, "views": 400},
        }

        def fake_fetch(status_id: str) -> dict:
            return {"name": status_id, "handle": status_id, "avatar_url": "", "url": f"https://x.com/i/status/{status_id}",
                    "text": "post", "quotes": 0, **stats[status_id]}

        with patch.object(slop, "_fetch", side_effect=fake_fetch):
            posts, shares = asyncio.run(slop.rank(self.bot, 5, 7))
        self.assertEqual([p.status_id for p in posts], ["big", "loved", "dud"])
        self.assertEqual(len(shares), 3)
        self.assertEqual(json.loads(self.bot.store.tweet_stats("big", 3600, now))["likes"], 50_000)  # cached

    def test_the_card_renders(self) -> None:
        share = {"sharer": "Santi", "reacts": 2, "guild_id": 10, "channel_id": 5, "message_id": 1}
        post = slop.Post("1", "https://x.com/i/status/1", [share], name="OpenAI", handle="OpenAI",
                         text="We're sharing a solution 🚀 " * 8, likes=120_400, reposts=20_100, views=75_100_000,
                         kind="hype", sloppiness=0.63)
        png = slop.render([post], "week", "sim-city", 12, [("Santi", 9)])
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertEqual(slop.compact(75_100_000), "75.1M")

    def test_link_text_and_urls_from_the_api_cannot_inject_markdown(self) -> None:
        share = {"sharer": "Santi", "reacts": 0, "guild_id": 10, "channel_id": 5, "message_id": 1}
        post = slop.Post("42", "https://evil.example/x](https://phish)", [share],
                         name="Free Nitro](https://phish.example) @everyone *")
        self.assertEqual(slop._safe_url(post), "https://x.com/i/status/42")
        label = slop._label(post)
        self.assertNotIn("](", label)
        self.assertNotIn("@everyone", label)
        post.url = "https://x.com/OpenAI/status/123"
        self.assertEqual(slop._safe_url(post), post.url)
