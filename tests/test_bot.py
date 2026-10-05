import asyncio
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from psychograph.bot import PsychographBot, is_allowed_channel
from psychograph.chess_game import Turn
from psychograph.cogs.chat import show_generation_failure
from psychograph.cogs.settings import StatusView, status_embed

from .helpers import FakeBackend, make_bot

ALLOWED = ("sim-city", "little-st-james", "shitpost", "games")


def interaction_in(channel_name: str, guild: object | None, kind=discord.InteractionType.application_command):
    response = SimpleNamespace(send_message=AsyncMock(), autocomplete=AsyncMock())
    return SimpleNamespace(channel=SimpleNamespace(name=channel_name, parent=None), guild=guild, type=kind, response=response)


class BotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeBackend()
        self.bot = make_bot(self.backend)
        asyncio.run(self.bot.load_cogs())

    def tearDown(self) -> None:
        self.bot.store.close()

    def test_slash_commands_are_registered(self) -> None:
        names = {command.name for command in self.bot.tree.get_commands()}

        self.assertTrue(
            {"persona", "persona-create", "persona-edit", "persona-delete", "reactions", "verbosity", "model", "cost", "status", "reset", "chess"}
            <= names
        )
        chess_group = self.bot.tree.get_command("chess")
        self.assertIn("commentary", {command.name for command in chess_group.commands})

    def test_channel_allowlist_follows_thread_parents(self) -> None:
        self.assertTrue(is_allowed_channel(SimpleNamespace(name="sim-city", parent=None), ALLOWED))
        self.assertTrue(is_allowed_channel(SimpleNamespace(name="Games", parent=None), ALLOWED))
        self.assertTrue(is_allowed_channel(SimpleNamespace(name="thread", parent=SimpleNamespace(name="games")), ALLOWED))
        self.assertFalse(is_allowed_channel(SimpleNamespace(name="general", parent=None), ALLOWED))
        self.assertFalse(is_allowed_channel(None, ALLOWED))

    def test_command_tree_blocks_channels_outside_allowlist(self) -> None:
        interaction = interaction_in("general", SimpleNamespace(id=1))

        self.assertFalse(asyncio.run(self.bot.tree.interaction_check(interaction)))
        interaction.response.send_message.assert_awaited_once()
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        self.assertIn("#sim-city", interaction.response.send_message.await_args.args[0])

    def test_command_tree_hides_autocomplete_outside_allowlist(self) -> None:
        interaction = interaction_in("general", SimpleNamespace(id=1), discord.InteractionType.autocomplete)

        self.assertFalse(asyncio.run(self.bot.tree.interaction_check(interaction)))
        interaction.response.autocomplete.assert_awaited_once_with([])
        interaction.response.send_message.assert_not_awaited()

    def test_command_tree_silently_blocks_dms(self) -> None:
        interaction = interaction_in("DM User", None)

        self.assertFalse(asyncio.run(self.bot.tree.interaction_check(interaction)))
        interaction.response.send_message.assert_not_awaited()

    def test_startup_clears_guild_commands_once(self) -> None:
        guild = SimpleNamespace(name="test", id=1)
        fake = SimpleNamespace(_legacy_guild_commands_cleared=False, guilds=[guild], tree=SimpleNamespace(sync=AsyncMock()))

        asyncio.run(PsychographBot.on_ready(fake))
        asyncio.run(PsychographBot.on_ready(fake))

        fake.tree.sync.assert_awaited_once_with(guild=guild)

    def test_deleted_failure_placeholder_does_not_raise(self) -> None:
        response = SimpleNamespace(status=404, reason="Not Found", headers={})
        placeholder = SimpleNamespace(edit=AsyncMock(side_effect=discord.NotFound(response, {"code": 10008, "message": "Unknown"})))

        with patch("psychograph.cogs.chat.log.info"):
            asyncio.run(show_generation_failure(placeholder, 1))

        placeholder.edit.assert_awaited_once()

    def test_status_embed_shows_channel_and_backend_settings(self) -> None:
        self.bot.store.update_channel(1, persona="charlie", verbosity="concise", persona_reactions=True)

        embed = status_embed(self.bot, 1, 10, "sim-city")

        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(embed.title, "#sim-city settings")
        self.assertEqual(fields["Persona"], "charlie")
        self.assertEqual(fields["Reply detail"], "Concise")
        self.assertEqual(fields["Persona reactions"], "On")
        self.assertEqual(fields["Context / output"], "4,096 / 512 tokens")
        self.assertEqual(fields["Model target"], "`test-model`")
        self.assertNotIn("Chess commentary", fields)

    def test_status_view_exposes_controls_and_hides_unauthorized_reaction_toggle(self) -> None:
        async def build() -> tuple[StatusView, StatusView]:
            return StatusView(self.bot, 1, 10, "sim-city", 100, True), StatusView(self.bot, 1, 10, "sim-city", 100, False)

        permitted, restricted = asyncio.run(build())
        labels = lambda view: {item.label for item in view.children if isinstance(item, discord.ui.Button)}

        self.assertEqual(len(permitted.persona_select.options), len(self.bot.personas.builtin_keys()) + 1)
        self.assertEqual(permitted.persona_select.options[0].value, "mochi")
        self.assertTrue(permitted.persona_select.options[0].default)
        self.assertIn("chess", {option.value for option in permitted.persona_select.options})
        self.assertTrue({"Detail: Balanced", "Reactions: Off", "Reset history"} <= labels(permitted))
        self.assertNotIn("Reactions: Off", labels(restricted))

    def test_status_view_cycles_verbosity_and_refreshes(self) -> None:
        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))

        async def press() -> StatusView:
            view = StatusView(self.bot, 1, 10, "sim-city", 100, True)
            await view.verbosity_button.callback(interaction)
            return view

        view = asyncio.run(press())

        interaction.response.edit_message.assert_awaited_once()
        self.assertEqual(self.bot.store.channel_settings(1).verbosity, "detailed")
        self.assertEqual(view.verbosity_button.label, "Detail: Detailed")


class ChatPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeBackend("Hello from the model.")
        self.bot = make_bot(self.backend)
        asyncio.run(self.bot.load_cogs())
        self.me = SimpleNamespace(id=999)
        self.bot._connection.user = self.me
        self.cog = self.bot.get_cog("ChatCog")
        self.sent_ids = iter(range(2000, 3000))

    def tearDown(self) -> None:
        self.bot.store.close()

    def message(self, content: str, message_id: int, reference=None, mentioned: bool = True, channel_name="sim-city"):
        posted = lambda **_: SimpleNamespace(id=next(self.sent_ids), add_reaction=AsyncMock())
        placeholder = SimpleNamespace(edit=AsyncMock(side_effect=posted))
        channel = SimpleNamespace(
            id=1,
            name=channel_name,
            parent=None,
            typing=nullcontext,
            send=AsyncMock(side_effect=posted),
            fetch_message=AsyncMock(return_value=SimpleNamespace(author=self.me)),
        )
        return SimpleNamespace(
            id=message_id,
            author=SimpleNamespace(id=5, bot=False),
            content=content,
            mentions=[self.me] if mentioned else [],
            reference=reference,
            channel=channel,
            guild=SimpleNamespace(id=10),
            embeds=[],
            reply=AsyncMock(return_value=placeholder),
        )

    def test_mention_replies_with_persona_and_records_the_exchange(self) -> None:
        self.bot.store.update_channel(1, persona="charlie", persona_reactions=True)
        message = self.message("<@999> what is difference?", 1000)

        asyncio.run(self.cog.on_message(message))

        system, user = self.backend.calls[0][0], self.backend.calls[0][-1]
        self.assertIn("Charlie", system["content"])
        self.assertEqual(user["content"], "User request: what is difference?")
        placeholder = message.reply.return_value
        embed = placeholder.edit.await_args.kwargs["embed"]
        self.assertEqual((embed.author.name, embed.description), ("charlie", "Hello from the model."))
        self.assertIn("(estimated)", embed.footer.text)
        chain = self.bot.store.message_chain(2000, 1)
        self.assertEqual([row["role"] for row in chain], ["user", "assistant"])

    def test_reply_to_the_bot_continues_the_chain(self) -> None:
        asyncio.run(self.cog.on_message(self.message("<@999> first", 1000)))
        reference = SimpleNamespace(message_id=2000, resolved=None)

        asyncio.run(self.cog.on_message(self.message("and then?", 1001, reference=reference, mentioned=False)))

        contents = [item["content"] for item in self.backend.calls[1][1:]]
        self.assertEqual(contents, ["User request: first", "Hello from the model.", "User request: and then?"])

    def test_ignores_unaddressed_messages_and_other_channels(self) -> None:
        asyncio.run(self.cog.on_message(self.message("just chatting", 1000, mentioned=False)))
        asyncio.run(self.cog.on_message(self.message("<@999> hi", 1001, channel_name="general")))

        self.assertEqual(self.backend.calls, [])

    def test_long_replies_split_into_a_linked_chain(self) -> None:
        self.backend.text = "word " * 1500
        message = self.message("<@999> go on", 1000)

        asyncio.run(self.cog.on_message(message))

        message.channel.send.assert_awaited_once()
        chain = self.bot.store.message_chain(2001, 1)
        self.assertEqual([row["role"] for row in chain], ["user", "assistant", "assistant"])
        self.assertEqual("".join(row["content"] for row in chain[1:]), self.backend.text.strip())

    def test_chess_channels_play_moves_instead_of_chatting(self) -> None:
        self.bot.store.update_channel(1, persona="chess")
        message = self.message("<@999> e4", 1000)

        with patch.object(self.bot.chess, "play", AsyncMock(return_value=Turn("**e4**  ·  **e5**", None))) as play:
            asyncio.run(self.cog.on_message(message))

        play.assert_awaited_once_with(1, "e4")
        self.assertEqual(self.backend.calls, [])
        self.assertIn("e5", message.reply.await_args.kwargs["embed"].description)

    def test_backend_failure_edits_the_placeholder(self) -> None:
        self.backend.complete = AsyncMock(side_effect=RuntimeError("down"))
        message = self.message("<@999> hi", 1000)

        with patch("psychograph.cogs.chat.log.exception"):
            asyncio.run(self.cog.on_message(message))

        self.assertIn("couldn't reach the model", message.reply.return_value.edit.await_args.kwargs["content"])


if __name__ == "__main__":
    unittest.main()
