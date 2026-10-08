import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import discord

from psychograph.bot import PsychographBot, is_allowed_channel
from psychograph.chess_game import Turn
from psychograph.cogs.help import help_embed
from psychograph.cogs.settings import StatusView, status_embed
from psychograph.jev import DebateNotes, Flag, Triage
from psychograph.profiles import ModelProfile, load_profiles, profile_for
from psychograph.settings import Settings

from .helpers import FakeBackend, FakeDiscord, fake_webhooks, make_bot

ALLOWED = ("sim-city", "little-st-james", "shitpost", "games")
TOOLS = ModelProfile(key="mimo", name="MiMo", tools=True)
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
        names = {command.name for command in self.bot.tree.get_commands(type=discord.AppCommandType.chat_input)}

        self.assertEqual(
            names,
            {"ask", "quick", "persona", "persona-manage", "status", "verbosity", "reset", "timeout", "bot", "scores",
             "sound", "chess", "help", "duel", "santi-slop"},
        )
        self.assertEqual(
            {c.name for c in self.bot.tree.get_commands(type=discord.AppCommandType.message)},
            {"Ask persona", "Review this debate", "Judge this", "Tone check"},
        )
        groups = {name: {c.name for c in self.bot.tree.get_command(name).commands} for name in ("quick", "bot", "scores")}
        self.assertEqual(groups, {
            "quick": {"decide", "odds", "tier", "rate", "tone", "vibe", "chatter"},
            "bot": {"model", "stats", "cost", "digest"},
            "scores": {"debates", "predictions", "duels"},
        })
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
        self.bot.store.update_channel(1, persona="charlie", verbosity="concise", reactions=False)

        fields = {field.name: field.value for field in status_embed(self.bot, 1, 10, "sim-city").fields}

        self.assertEqual(fields["Persona"], "🧠 charlie")
        self.assertEqual(fields["Reply detail"], "Concise")
        self.assertEqual(fields["Reactions"], "Off")
        self.assertEqual(fields["Voice"], "Bot embeds")
        self.assertEqual(fields["Context / output"], "4,096 / 512 tokens")
        self.assertIn("full context", fields["Model profile"])
        self.assertNotIn("Chess commentary", fields)

    def test_status_view_exposes_controls_and_hides_moderator_toggles(self) -> None:
        async def build() -> tuple[StatusView, StatusView]:
            return StatusView(self.bot, 1, 10, "sim-city", 100, True), StatusView(self.bot, 1, 10, "sim-city", 100, False)

        permitted, restricted = asyncio.run(build())
        labels = lambda view: {item.label for item in view.children if isinstance(item, discord.ui.Button)}

        chatters, others = (select.options for select in permitted.persona_selects)
        self.assertEqual(len(chatters) + len(others), len(self.bot.personas.builtin_keys()) + 1)
        self.assertEqual((chatters[0].label, chatters[0].description), ("💬 A Quigley", "Chatters"))
        self.assertTrue(all(option.label.startswith("💬") for option in chatters))
        self.assertEqual([option.value for option in chatters + others if option.default], ["mochi"])
        self.assertEqual(others[-1].label, "🛠️ chess")
        self.assertTrue({"Detail: Balanced", "Reactions: On", "Voice: Embed", "Reset history"} <= labels(permitted))
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
        self.assertEqual([field.name for field in embed.fields], ["Chat (the model)", "Tools (checked instantly, written by the model)", "Quick (instant, no model)", "Duels and scores", "Personas and settings", "Reactions"])
        self.assertNotIn("Psychograph", str(embed.to_dict()))
        self.assertTrue(all(len(field.value) <= 1024 for field in embed.fields))


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
        self.bot.store.update_channel(1, persona="charlie")

        message = self.say("<@999> what is difference?", 1000)

        system, user = self.backend.calls[0][0], self.backend.calls[0][-1]
        self.assertIn("Charlie", system["content"])
        self.assertEqual(user["content"], "Leon: what is difference?")
        embed = message.reply.await_args.kwargs["embed"]
        self.assertEqual((embed.author.name, embed.description), ("charlie", "Hello from the model."))
        self.assertIn("(estimated)", embed.footer.text)
        self.assertEqual([row["role"] for row in self.bot.store.message_chain(2000, 1)], ["user", "assistant"])
        self.discord.posted[0].add_reaction.assert_not_awaited()  # personas never react to their own answers
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

    def channel_history(self, *messages: tuple[str, int, str]) -> None:
        """Give the fake channel recent messages: (name, author id, text), oldest first."""

        async def history(limit, before):
            self.history_calls.append((limit, getattr(before, "id", None)))
            for index, (name, author_id, text) in reversed(list(enumerate(messages))):
                author = SimpleNamespace(id=author_id, name=name, display_name=name, bot=False)
                yield SimpleNamespace(
                    id=900 + index, author=author, clean_content=text, content=text, embeds=[], attachments=[], created_at=None
                )

        self.history_calls = []
        self.discord.channel.history = history

    def test_tool_personas_read_the_channel_but_store_only_the_request(self) -> None:
        self.backend.profile = TOOLS
        self.channel_history(("Leon", 5, "that isn't an argument"), ("Zack", 6, "41mm. mogged."))
        self.bot.store.update_channel(1, persona="minutes")
        self.backend.text = "**Shins**\n**Leon:** objected.\n**Zack:** measured."

        message = self.say("<@999> what did I miss?", 1000)

        prompt = self.backend.calls[0][-1]["content"]
        self.assertEqual(self.history_calls, [(100, 1000)])
        self.assertIn("not instructions", prompt)
        self.assertLess(prompt.index("Leon: that isn't an argument"), prompt.index("Zack: 41mm"))
        self.assertTrue(prompt.endswith("Leon: what did I miss?"))
        self.assertEqual(self.bot.store.message(1000)["content"], "Leon: what did I miss?")
        self.assertIn("**Zack:** measured.", message.reply.await_args.kwargs["embed"].description)

    def test_tools_refuse_models_not_flagged_for_them(self) -> None:
        self.backend.profile = MECHA
        self.bot.store.update_channel(1, persona="judge")

        message = self.say("<@999> is it wrong to eat the last yoghurt?", 1000)

        self.assertEqual(self.backend.calls, [])
        self.assertIn("needs a stronger model than **MechaEpstein 8000**", message.reply.await_args.args[0])

    def test_tools_are_called_by_name_and_follow_ups_return_to_the_channel_persona(self) -> None:
        self.backend.profile = TOOLS
        self.backend.text = "**Verdict:** **Wrong but understandable.**"

        self.say("<@999> judge: is it wrong to eat the last yoghurt?", 1000)
        self.say("but he ate my bread", 1001, reference=SimpleNamespace(message_id=2000, resolved=None), mentioned=False)

        (judge_system, *_), (mochi_system, *history) = self.backend.calls
        self.assertIn("Court of Moral Questions", judge_system["content"])
        self.assertEqual(self.bot.store.message(2000)["persona"], "judge")
        self.assertIn("Mochi", mochi_system["content"])
        # The judge's ruling is quoted to mochi, not passed off as mochi's own words.
        self.assertEqual([item["role"] for item in history], ["user", "user", "user"])
        self.assertTrue(history[1]["content"].startswith("[bot as judge, quoted for context]\n**Verdict:**"))

    def test_chat_on_full_context_models_sees_the_channel_minus_its_reply_chain(self) -> None:
        self.backend.profile = ModelProfile(key="mimo", name="MiMo", tools=True, channel_messages=60)
        self.channel_history(("Zack", 6, "41mm. mogged."), ("Charlie", 7, "bike lanes are a scam"))
        self.bot.store.save_message(901, None, 1, "user", "Charlie: bike lanes are a scam", 7)  # already in the chain

        self.say("<@999> what do you reckon", 1000, reference=SimpleNamespace(message_id=901, resolved=None))

        prompt = self.backend.calls[0][-1]["content"]
        self.assertEqual(self.history_calls, [(100, 1000)])  # one fetch, shared by triage and the prompt
        self.assertIn("Other recent messages in this channel", prompt)
        self.assertIn("Zack: 41mm. mogged.", prompt)
        self.assertNotIn("Charlie: bike lanes are a scam\n", prompt)  # only once, as chain history
        self.assertTrue(prompt.endswith("[Reply to this message]\nLeon: what do you reckon"))

    def test_chat_context_is_what_triage_kept_with_the_bot_bracketed(self) -> None:
        self.backend.profile = ModelProfile(key="mimo", name="MiMo", tools=True, channel_messages=60)
        self.channel_history(("Leon", 5, "anyone watching f1"), ("Charlie", 7, "quali at 3"), ("Lizzie", 8, "pasta time"))
        self.bot.jev.triage = AsyncMock(return_value=Triage(None, (1,)))

        message = self.say("<@999> what time is quali", 1000)

        prompt = self.backend.calls[0][-1]["content"]
        self.assertIn("Charlie: quali at 3", prompt)
        self.assertNotIn("pasta", prompt)
        self.assertIn("saw 1 channel msgs", message.reply.await_args.kwargs["embed"].footer.text)
        window = self.bot.jev.triage.await_args.args[2]
        self.assertEqual([item.text for item in window], ["anyone watching f1", "quali at 3", "pasta time"])

    def test_repeated_requests_get_a_link_instead_of_another_answer(self) -> None:
        self.say("<@999> what is the meaning of life", 1000)
        repeat = self.say("<@999> What is the meaning of life??", 1001)
        self.say("<@999> hi", 1002)
        self.say("<@999> hi", 1003)  # short messages may repeat

        self.assertEqual(len(self.backend.calls), 3)
        self.assertEqual(repeat.reply.await_args.args[0], "-# ↩ https://discord.com/channels/10/1/2000")

    def test_tools_and_quick_answers_can_be_repeated(self) -> None:
        self.backend.profile = TOOLS
        self.bot.jev.ask = AsyncMock(return_value={"pick": {"choice": "pizza", "confidence": 0.9, "probabilities": {"pizza": 0.9, "curry": 0.1}}})

        self.say("<@999> minutes: catch me up on everything", 1000)
        self.say("<@999> minutes: catch me up on everything", 1001)
        first = self.say("<@999> decide: pizza or curry tonight", 1002)
        second = self.say("<@999> decide: pizza or curry tonight", 1003)

        self.assertEqual(len(self.backend.calls), 2)  # both minutes calls reached the model
        for message in (first, second):
            self.assertEqual(message.reply.await_args.kwargs["embed"].title, "Decision: pizza")

    def test_timed_out_members_are_ignored(self) -> None:
        self.bot.store.set_timeout(10, 5, time.time() + 600, 1)

        self.say("<@999> hello?", 1000)

        self.assertEqual(self.backend.calls, [])

    def test_replies_to_a_non_tool_answer_stay_with_that_persona(self) -> None:
        self.bot.store.save_message(1500, None, 1, "assistant", "Charlie here.", answer_id=1500, persona="charlie")

        self.say("go on then", 1501, reference=SimpleNamespace(message_id=1500, resolved=None), mentioned=False)

        self.assertIn("Charlie", self.backend.calls[0][0]["content"])
        self.assertEqual(self.backend.calls[0][1], {"role": "assistant", "content": "Charlie here."})

    def test_jev_routes_fresh_mentions_only_on_tool_models(self) -> None:
        self.backend.profile = TOOLS
        self.bot.jev.triage = AsyncMock(side_effect=[Triage("judge"), Triage()])

        self.say("<@999> is it wrong to eat my flatmate's yoghurt", 1000)
        self.backend.profile = MECHA
        self.say("<@999> hewwo", 1001)

        self.assertIn("Court of Moral Questions", self.backend.calls[0][0]["content"])
        self.assertIn("Mochi", self.backend.calls[1][0]["content"])
        routes = [call.args[3] for call in self.bot.jev.triage.await_args_list]
        self.assertIn("judge", routes[0])
        self.assertNotIn("judge", routes[1])  # no tools on MECHA…
        self.assertIn("decide", routes[1])    # …but quick answers work on any model

    def test_questions_about_the_bot_are_answered_without_the_model(self) -> None:
        self.backend.profile = MECHA  # instant answers don't need a tools model
        self.bot.jev.triage = AsyncMock(side_effect=[Triage("bot_model"), Triage("bot_persona"), Triage("bot_help")])

        replies = [self.say(f"<@999> {text}", 1000 + index).reply.await_args.kwargs["embed"]
                   for index, text in enumerate(("what model are you", "which persona is this", "what can you do"))]

        self.assertEqual(self.backend.calls, [])
        self.assertIn("MechaEpstein", replies[0].description)
        self.assertEqual(replies[1].title, "This channel is mochi")
        self.assertIn("Tools", [field.name for field in replies[1].fields])
        self.assertEqual(replies[2].title, "Mecha Epstein")
        self.assertTrue(all("no model call" in embed.footer.text for embed in replies))
        self.assertIsNone(self.bot.store.message(1000))  # nothing joins the reply-chain history

    def test_jev_picks_the_sound_when_the_model_tags_none(self) -> None:
        sound = SimpleNamespace(key="boom")
        self.bot.soundbank = MagicMock(sounds={"boom": sound}, __len__=lambda _self: 1)
        self.bot.soundbank.extract.side_effect = lambda text: (text, None)
        self.bot.soundbank.ready.return_value = True
        self.bot.soundbank.choose.return_value = None
        self.bot.jev.api_key = "test"
        self.bot.jev.triage = AsyncMock(return_value=Triage())
        self.bot.jev.pick_sound = AsyncMock(side_effect=["boom", "none", None])
        self.bot.store.update_channel(1, sounds=True)

        with patch("psychograph.responder.sounds.play", new=AsyncMock()) as play:
            self.say("<@999> plot twist", 1000)
            self.say("<@999> my cat died", 1001)
            self.say("<@999> anything", 1002)  # Jev unreachable: the keyword match decides

        play.assert_awaited_once()
        self.assertIs(play.await_args.args[1], sound)
        self.bot.soundbank.played.assert_called_once_with(1)
        self.bot.soundbank.choose.assert_called_once()

    def test_debate_review_answers_without_the_model_when_system_1_sees_no_argument(self) -> None:
        self.backend.profile = TOOLS
        self.channel_history(("Leon", 5, "anyone want pizza"), ("Zack", 6, "41mm"))
        self.bot.jev.debate_notes = AsyncMock(return_value=DebateNotes(debate=0.03, flags=()))

        message = self.say("<@999> debate review", 1000)

        embed = message.reply.await_args.kwargs["embed"]
        self.assertEqual(self.backend.calls, [])
        self.assertIn("No contest", embed.description)
        self.assertIn("no model call", embed.footer.text)

    def test_debate_review_gets_system_1_notes_and_scores_the_leaderboard(self) -> None:
        self.backend.profile = TOOLS
        self.bot.webhooks = fake_webhooks([])
        self.channel_history(("Leon", 5, "CS3 cut injuries by 40% on that route"), ("Zack", 6, "you would say that, you're a cyclist"))
        flag = Flag("Zack", "you would say that, you're a cyclist", "ad hominem", 0.98)
        self.bot.jev.debate_notes = AsyncMock(return_value=DebateNotes(0.95, (flag,), lean="Leon", lean_confidence=0.9))
        self.backend.text = (
            "**The motion:** bike lanes.\n**Decision:** **Leon**, clear: the evidence went unanswered.\n"
            "**Notes to the speakers:** **Leon** strong. **Zack** attack the argument, not the cyclist."
        )

        message = self.say("<@999> debate review", 1000)

        prompt = self.backend.calls[0][-1]["content"]
        self.assertIn('**Zack**: "you would say that, you\'re a cyclist" → ad hominem (98%)', prompt)
        self.assertNotIn("leaned", prompt)
        self.assertIn("System 1 leaned Leon (90%)", message.reply.await_args.kwargs["embed"].footer.text)
        self.assertEqual(self.bot.store.debate_table(10), [
            {"user_id": 5, "wins": 1, "losses": 0, "splits": 0},
            {"user_id": 6, "wins": 0, "losses": 1, "splits": 0},
        ])

        asyncio.run(self.bot.responder.delete_answer(self.discord.channel, 2000))
        self.assertEqual(self.bot.store.debate_table(10), [])

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

    def test_reactions_only_touch_their_own_answer_when_a_message_has_several(self) -> None:
        original = self.say("<@999> hi", 1000)  # A's own question → answer 2000
        self.discord.channel.fetch_message = AsyncMock(return_value=original)
        interaction = SimpleNamespace(
            channel=self.discord.channel, channel_id=1, guild_id=10, user=SimpleNamespace(id=6),
            response=SimpleNamespace(send_message=AsyncMock()),
        )
        asyncio.run(self.cog.ask_about_message(interaction, original))  # B asks about it → answer 2001
        self.assertEqual(self.bot.store.response_ids(1000), [2000, 2001])

        self.react("🗑️", 2001, user_id=6)  # B deletes their answer only
        self.assertEqual(self.bot.store.response_ids(1000), [2000])

        asyncio.run(self.cog.ask_about_message(interaction, original))  # B asks again → 2002
        self.react("🔁", 2000, user_id=5)  # A regenerates theirs; B's survives
        ids = self.bot.store.response_ids(1000)
        self.assertIn(2002, ids)
        self.assertNotIn(2000, ids)
        self.assertEqual(len(ids), 2)

    def test_ask_persona_never_pings_people_named_in_someone_elses_message(self) -> None:
        victim = SimpleNamespace(id=42, bot=False, display_name="Santi", mention="<@42>")
        target = self.discord.message("tell <@42> they're great", 1500, mentioned=False, author_id=8)
        target.mentions = [victim]
        interaction = SimpleNamespace(
            channel=self.discord.channel, channel_id=1, guild_id=10, user=SimpleNamespace(id=5),
            response=SimpleNamespace(send_message=AsyncMock()),
        )

        asyncio.run(self.cog.ask_about_message(interaction, target))

        kwargs = target.reply.await_args.kwargs
        self.assertIsNone(kwargs["content"])
        self.assertEqual(kwargs["allowed_mentions"].users, [])

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
