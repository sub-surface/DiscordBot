import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from psychograph.bot import PsychographBot, is_allowed_channel
from psychograph.chess_game import Turn
from psychograph.cogs.help import help_embed
from psychograph.cogs.settings import StatusView, status_embed
from psychograph.profiles import load_profiles, profile_for
from psychograph.settings import Settings

from .helpers import FakeBackend, FakeDiscord, fake_webhooks, make_bot

ALLOWED = ("sim-city", "little-st-james", "shitpost", "games")
MECHA = profile_for("mradermacher/MechaEpstein-8000-GGUF/MechaEpstein-8000.Q8_0.gguf", load_profiles(Settings().models_file))


def interaction_in(channel_name: str, guild: object | None, kind=discord.InteractionType.application_command):
    response = SimpleNamespace(send_message=AsyncMock(), autocomplete=AsyncMock())
    return SimpleNamespace(channel=SimpleNamespace(name=channel_name, parent=None), guild=guild, type=kind, response=response)


class BotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = make_bot()
        asyncio.run(self.bot.load_cogs())

    def tearDown(self) -> None:
        self.bot.store.close()

    def test_commands_are_registered(self) -> None:
        names = {command.name for command in self.bot.tree.get_commands()}

        self.assertTrue(
            {"persona", "persona-create", "persona-edit", "persona-delete", "reactions", "verbosity", "model",
             "cost", "stats", "status", "reset", "chess", "ask", "help"}
            <= names
        )
        self.assertIn("Ask persona", {c.name for c in self.bot.tree.get_commands(type=discord.AppCommandType.message)})
        self.assertIn("commentary", {command.name for command in self.bot.tree.get_command("chess").commands})

    def test_channel_allowlist_follows_thread_parents(self) -> None:
        self.assertTrue(is_allowed_channel(SimpleNamespace(name="sim-city", parent=None), ALLOWED))
        self.assertTrue(is_allowed_channel(SimpleNamespace(name="Games", parent=None), ALLOWED))
        self.assertTrue(is_allowed_channel(SimpleNamespace(name="thread", parent=SimpleNamespace(name="games")), ALLOWED))
        self.assertFalse(is_allowed_channel(SimpleNamespace(name="general", parent=None), ALLOWED))
        self.assertFalse(is_allowed_channel(None, ALLOWED))

    def test_command_tree_blocks_channels_outside_allowlist(self) -> None:
        interaction = interaction_in("general", SimpleNamespace(id=1))

        self.assertFalse(asyncio.run(self.bot.tree.interaction_check(interaction)))
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

    def test_startup_sets_presence_and_clears_guild_commands_once(self) -> None:
        guild = SimpleNamespace(name="test", id=1)
        fake = SimpleNamespace(
            _legacy_guild_commands_cleared=False,
            guilds=[guild],
            tree=SimpleNamespace(sync=AsyncMock()),
            change_presence=AsyncMock(),
            presence=lambda: "presence",
        )

        asyncio.run(PsychographBot.on_ready(fake))
        asyncio.run(PsychographBot.on_ready(fake))

        fake.tree.sync.assert_awaited_once_with(guild=guild)
        fake.change_presence.assert_awaited_with(activity="presence")

    def test_presence_names_the_model_profile(self) -> None:
        bot = make_bot(FakeBackend(profile=MECHA))

        self.assertEqual(bot.presence().name, "🧠 MechaEpstein 8000 · compact context · /help")
        bot.store.close()

    def test_status_embed_shows_channel_and_backend_settings(self) -> None:
        self.bot.store.update_channel(1, persona="charlie", verbosity="concise", persona_reactions=True)

        fields = {field.name: field.value for field in status_embed(self.bot, 1, 10, "sim-city").fields}

        self.assertEqual(fields["Persona"], "🧠 charlie")
        self.assertEqual(fields["Reply detail"], "Concise")
        self.assertEqual(fields["Persona reactions"], "On")
        self.assertEqual(fields["Voice"], "Bot embeds")
        self.assertEqual(fields["Context / output"], "4,096 / 512 tokens")
        self.assertIn("full context", fields["Model profile"])
        self.assertNotIn("Chess commentary", fields)

    def test_status_view_exposes_controls_and_hides_moderator_toggles(self) -> None:
        async def build() -> tuple[StatusView, StatusView]:
            return StatusView(self.bot, 1, 10, "sim-city", 100, True), StatusView(self.bot, 1, 10, "sim-city", 100, False)

        permitted, restricted = asyncio.run(build())
        labels = lambda view: {item.label for item in view.children if isinstance(item, discord.ui.Button)}

        self.assertEqual(len(permitted.persona_select.options), len(self.bot.personas.builtin_keys()) + 1)
        self.assertEqual(permitted.persona_select.options[0].value, "mochi")
        self.assertTrue({"Detail: Balanced", "Reactions: Off", "Voice: Embed", "Reset history"} <= labels(permitted))
        self.assertEqual(labels(restricted), {"Detail: Balanced", "Reset history"})

    def test_status_view_cycles_verbosity_and_refreshes(self) -> None:
        interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))

        async def press() -> StatusView:
            view = StatusView(self.bot, 1, 10, "sim-city", 100, True)
            await view.verbosity_button.callback(interaction)
            return view

        view = asyncio.run(press())

        self.assertEqual(self.bot.store.channel_settings(1).verbosity, "detailed")
        self.assertEqual(view.verbosity_button.label, "Detail: Detailed")

    def test_voice_toggle_requires_webhook_permission(self) -> None:
        send = AsyncMock()
        interaction = SimpleNamespace(
            permissions=SimpleNamespace(manage_messages=True),
            channel=SimpleNamespace(),
            response=SimpleNamespace(send_message=send, edit_message=AsyncMock()),
        )

        async def press(available: bool) -> None:
            self.bot.webhooks = fake_webhooks([])
            self.bot.webhooks.available.return_value = available
            view = StatusView(self.bot, 1, 10, "sim-city", 100, True)
            await view.voice_button.callback(interaction)

        asyncio.run(press(False))
        self.assertIn("Manage Webhooks", send.await_args.args[0])
        self.assertFalse(self.bot.store.channel_settings(1).persona_voice)

        asyncio.run(press(True))
        self.assertTrue(self.bot.store.channel_settings(1).persona_voice)

    def test_help_describes_the_channel_persona(self) -> None:
        embed = help_embed(self.bot, 1, 10)

        self.assertIn("mochi", embed.description)
        self.assertIn("🔁", "".join(field.value for field in embed.fields))


class ChatPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeBackend("Hello from the model.")
        self.bot = make_bot(self.backend)
        asyncio.run(self.bot.load_cogs())
        self.me = SimpleNamespace(id=999)
        self.bot._connection.user = self.me
        self.cog = self.bot.get_cog("ChatCog")
        self.discord = FakeDiscord(self.me)

    def tearDown(self) -> None:
        self.bot.store.close()

    def say(self, *args, **kwargs):
        message = self.discord.message(*args, **kwargs)
        asyncio.run(self.cog.on_message(message))
        return message

    def test_mention_replies_as_the_persona_and_records_the_exchange(self) -> None:
        self.bot.store.update_channel(1, persona="charlie", persona_reactions=True)

        message = self.say("<@999> what is difference?", 1000)

        system, user = self.backend.calls[0][0], self.backend.calls[0][-1]
        self.assertIn("Charlie", system["content"])
        self.assertEqual(user["content"], "Leon: what is difference?")
        embed = message.reply.await_args.kwargs["embed"]
        self.assertEqual((embed.author.name, embed.description), ("charlie", "Hello from the model."))
        self.assertIn("(estimated)", embed.footer.text)
        self.assertEqual([row["role"] for row in self.bot.store.message_chain(2000, 1)], ["user", "assistant"])
        self.discord.posted[0].add_reaction.assert_awaited_once_with("🧠")
        message.add_reaction.assert_awaited_once_with("👀")
        message.remove_reaction.assert_awaited_once_with("👀", self.me)

    def test_status_reaction_shows_cold_start_and_queue(self) -> None:
        self.backend.likely_cold = True
        self.assertEqual(self.say("<@999> hi", 1000).add_reaction.await_args.args[0], "☁️")

        self.backend.likely_cold, self.backend.in_flight = False, 1
        self.assertEqual(self.say("<@999> hi", 1001).add_reaction.await_args.args[0], "⏳")
        self.assertEqual(self.bot.store.generation_stats()["cold_starts"], 1)

    def test_reply_to_the_bot_continues_the_chain(self) -> None:
        self.say("<@999> first", 1000)

        self.say("and then?", 1001, reference=SimpleNamespace(message_id=2000, resolved=None), mentioned=False)

        contents = [item["content"] for item in self.backend.calls[1][1:]]
        self.assertEqual(contents, ["Leon: first", "Hello from the model.", "Leon: and then?"])

    def test_ignores_unaddressed_messages_and_other_channels(self) -> None:
        self.say("just chatting", 1000, mentioned=False)
        self.discord.channel.name = "general"
        self.say("<@999> hi", 1001)

        self.assertEqual(self.backend.calls, [])

    def test_compact_profile_sends_a_short_clipped_context(self) -> None:
        self.backend.profile = MECHA
        self.say("<@999> " + "tell me everything " * 60, 1000)

        self.say("<@999> and?", 1001, reference=SimpleNamespace(message_id=2000, resolved=None))

        system, *history = self.backend.calls[1]
        self.assertLess(len(system["content"]), 1300)
        self.assertIn("Write only mochi's reply", system["content"])
        self.assertLessEqual(len(history[0]["content"]), 410)
        self.assertTrue(history[0]["content"].endswith("…"))

    def test_model_output_is_cleaned_before_posting(self) -> None:
        self.backend.text = "<think>plan</think>mochi: hewwo~<|im_end|>\nLeon: now I talk for Leon"

        message = self.say("<@999> hi", 1000)

        self.assertEqual(message.reply.await_args.kwargs["embed"].description, "hewwo~")

    def test_long_replies_split_into_a_linked_chain(self) -> None:
        self.backend.text = "word " * 1500

        self.say("<@999> go on", 1000)

        self.discord.channel.send.assert_awaited_once()
        chain = self.bot.store.message_chain(2001, 1)
        self.assertEqual([row["role"] for row in chain], ["user", "assistant", "assistant"])
        self.assertEqual("".join(row["content"] for row in chain[1:]), self.backend.text.strip())
        self.assertEqual(self.bot.store.response_ids(1000), [2000, 2001])

    def test_persona_voice_posts_through_the_webhook(self) -> None:
        posted: list = []
        self.bot.webhooks = fake_webhooks(posted)
        self.bot.store.update_channel(1, persona="charlie", persona_voice=True)

        message = self.say("<@999> hi", 1000)

        message.reply.assert_not_awaited()
        self.assertEqual(posted[0].persona, "charlie")
        self.assertTrue(posted[0].content.startswith("-# ↪ [Leon]"))
        self.assertTrue(posted[0].content.endswith("Hello from the model."))
        self.assertEqual(self.bot.store.message(5000)["content"], "Hello from the model.")

    def test_reply_to_a_persona_voice_message_continues_the_chain(self) -> None:
        self.bot.webhooks = fake_webhooks([])
        self.bot.store.update_channel(1, persona_voice=True)
        self.say("<@999> first", 1000)
        self.discord.channel.fetch_message.side_effect = AssertionError("stored answers need no fetch")

        self.say("more", 1001, reference=SimpleNamespace(message_id=5000, resolved=None), mentioned=False)

        self.assertEqual(len(self.backend.calls), 2)

    def test_chess_channels_play_moves_instead_of_chatting(self) -> None:
        self.bot.store.update_channel(1, persona="chess")

        with patch.object(self.bot.chess, "play", AsyncMock(return_value=Turn("**e4**  ·  **e5**", None))) as play:
            message = self.say("<@999> e4", 1000)

        play.assert_awaited_once_with(1, "e4")
        self.assertEqual(self.backend.calls, [])
        self.assertIn("e5", message.reply.await_args.kwargs["embed"].description)

    def test_backend_failure_reports_and_logs(self) -> None:
        self.backend.complete = AsyncMock(side_effect=RuntimeError("down"))

        with patch("psychograph.responder.log.exception"):
            message = self.say("<@999> hi", 1000)

        self.assertIn("couldn't reach the model", message.reply.await_args.args[0])
        self.assertIn("⚠️", [call.args[0] for call in message.add_reaction.await_args_list])
        self.assertEqual(self.bot.store.generation_stats()["failures"], 1)

    def react(self, emoji: str, message_id: int, user_id: int, manage_messages: bool = False) -> None:
        self.discord.channel.permissions_for = lambda member: SimpleNamespace(manage_messages=manage_messages)
        payload = SimpleNamespace(emoji=emoji, message_id=message_id, user_id=user_id, channel_id=1, member=SimpleNamespace())
        with patch.object(self.bot, "get_channel", return_value=self.discord.channel):
            asyncio.run(self.cog.on_raw_reaction_add(payload))

    def test_asker_can_regenerate_an_answer(self) -> None:
        original = self.say("<@999> hi", 1000)
        self.discord.channel.fetch_message = AsyncMock(return_value=original)
        self.backend.text = "A better answer."

        self.react("🔁", 2000, user_id=42)
        self.assertEqual(len(self.backend.calls), 1)  # not the asker

        self.react("🔁", 2000, user_id=5)

        self.assertEqual(len(self.backend.calls), 2)
        self.assertIsNone(self.bot.store.message(2000))
        self.assertEqual(self.bot.store.message(self.bot.store.response_ids(1000)[0])["content"], "A better answer.")

    def test_delete_reaction_needs_asker_or_moderator(self) -> None:
        self.say("<@999> hi", 1000)

        self.react("🗑️", 2000, user_id=42)
        self.assertIsNotNone(self.bot.store.message(2000))

        self.react("🗑️", 2000, user_id=42, manage_messages=True)
        self.assertIsNone(self.bot.store.message(2000))
        self.assertEqual(self.bot.store.response_ids(1000), [])

    def test_ask_persona_answers_belong_to_whoever_asked_not_the_quoted_author(self) -> None:
        target = self.discord.message("pineapple on pizza is fine", 1500, mentioned=False, author_id=8)
        self.discord.channel.fetch_message = AsyncMock(return_value=target)
        interaction = SimpleNamespace(
            channel=self.discord.channel, channel_id=1, guild_id=10, user=SimpleNamespace(id=5),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        asyncio.run(self.cog.ask_about_message(interaction, target))
        self.assertEqual(self.bot.store.message(2000)["requester_id"], 5)

        self.react("🔁", 2000, user_id=8)  # the quoted author can't replay their message at the bot
        self.react("🔁", 2000, user_id=5)  # nor can the requester: it isn't their message
        self.react("🗑️", 2000, user_id=8)
        self.assertEqual(len(self.backend.calls), 1)
        self.assertIsNotNone(self.bot.store.message(2000))

        self.react("🗑️", 2000, user_id=5)
        self.assertIsNone(self.bot.store.message(2000))

    def test_ask_command_answers_as_another_persona_via_followup(self) -> None:
        followup = SimpleNamespace(send=AsyncMock(side_effect=self.discord._post))
        interaction = SimpleNamespace(
            id=777,
            guild_id=10,
            channel=self.discord.channel,
            channel_id=1,
            user=SimpleNamespace(id=5, name="leon", display_name="Leon"),
            response=SimpleNamespace(defer=AsyncMock(), send_message=AsyncMock()),
            followup=followup,
        )

        asyncio.run(self.cog.ask.callback(self.cog, interaction, "pineapple", "rate my fit"))

        self.assertEqual(followup.send.await_args.kwargs["embed"].author.name, "pineapple")
        self.assertIn("pineapple", self.backend.calls[0][0]["content"].casefold())
        self.assertEqual(self.bot.store.channel_settings(1).persona, None)
        self.assertEqual(self.bot.store.response_ids(777), [2000])

    def test_message_command_responds_to_someone_elses_message(self) -> None:
        target = self.discord.message("pineapple on pizza is fine", 1500, mentioned=False, author_id=8)
        interaction = SimpleNamespace(
            channel=self.discord.channel,
            channel_id=1,
            guild_id=10,
            user=SimpleNamespace(id=5),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        asyncio.run(self.cog.ask_about_message(interaction, target))

        self.assertEqual(self.backend.calls[0][-1]["content"], "Leon: pineapple on pizza is fine")
        target.reply.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
