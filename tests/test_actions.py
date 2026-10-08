import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from psychograph import actions
from psychograph.cogs.ops import OpsCommands

from .helpers import make_bot


def interaction(administrator: bool, guild: bool = True, data: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        type=discord.InteractionType.application_command,
        data=data or {},
        guild=SimpleNamespace(id=10) if guild else None,
        channel=SimpleNamespace(name="sim-city", id=1),
        user=SimpleNamespace(id=5, display_name="Hugh Chungus"),
        permissions=SimpleNamespace(administrator=administrator, manage_guild=administrator),
        command=SimpleNamespace(qualified_name="bot cost"),
        message=None,
        response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


class ActionLogTests(unittest.TestCase):
    def test_a_line_is_sigil_channel_member_then_what(self) -> None:
        with self.assertLogs("psychograph.act") as logs:
            actions.act("+", SimpleNamespace(name="sim-city"), None, "debate Pearl > Zack")
            actions.act("x", None, 132301139667451904, "answer 1 (2 msgs)")
        self.assertEqual(logs.records[0].getMessage(), "+ #sim-city     -              debate Pearl > Zack")
        self.assertEqual(logs.records[1].getMessage(), "x dm            132301139667451904 answer 1 (2 msgs)")

    def test_commands_are_logged_with_their_options_as_typed(self) -> None:
        data = {"name": "bot", "type": 1, "options": [
            {"name": "thinking", "type": 1, "options": [{"name": "mode", "type": 3, "value": "on"}]},
        ]}
        with self.assertLogs("psychograph.act") as logs:
            actions.interaction(interaction(True, data=data))
            actions.interaction(interaction(True, data={"name": "ask", "type": 1, "options": [
                {"name": "question", "type": 3, "value": "who won"}, {"name": "private", "type": 5, "value": True},
            ]}))
            actions.interaction(interaction(True, data={"name": "Review this debate", "type": 3, "target_id": "77"}))
        self.assertTrue(logs.records[0].getMessage().endswith("bot thinking mode=on"))
        self.assertTrue(logs.records[1].getMessage().endswith("ask question='who won' private=True"))
        self.assertTrue(logs.records[2].getMessage().endswith("Review this debate on 77"))


class AdminSettingsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = make_bot()
        self.bot.is_owner = AsyncMock(return_value=False)
        self.cog = OpsCommands(self.bot)

    def tearDown(self) -> None:
        self.bot.store.close()

    def test_admins_and_the_owner_can_switch_thinking_but_not_managers_or_dms(self) -> None:
        with self.assertLogs("psychograph.act") as logs:
            asyncio.run(self.cog.thinking.callback(self.cog, interaction(False), "on"))
        self.assertFalse(self.bot.backend.thinking)
        self.assertIn("! #sim-city", logs.records[0].getMessage())
        self.assertIn("bot cost: not an admin", logs.records[0].getMessage())

        asyncio.run(self.cog.thinking.callback(self.cog, interaction(True), "on"))
        self.assertTrue(self.bot.backend.thinking)

        with self.assertLogs("psychograph.act"):
            asyncio.run(self.cog.thinking.callback(self.cog, interaction(True, guild=False), "off"))
        self.assertTrue(self.bot.backend.thinking)  # a DM has no permissions to check

        self.bot.is_owner = AsyncMock(return_value=True)
        asyncio.run(self.cog.thinking.callback(self.cog, interaction(False, guild=False), "off"))
        self.assertFalse(self.bot.backend.thinking)

    def test_cost_is_refused_to_non_admins(self) -> None:
        item = interaction(False)
        with self.assertLogs("psychograph.act"):
            asyncio.run(self.cog.cost.callback(self.cog, item))
        self.assertIn("Only server admins", item.response.send_message.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
