import asyncio
import base64
import json
import random
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from psychograph import sounds
from psychograph.sounds import Soundbank, VoiceMessageFile

from .helpers import FakeBackend, FakeDiscord, fake_webhooks, make_bot

SOUNDS = [
    {"key": "vine_boom", "keywords": ["no way", "reveal"], "mood": "shocked", "description": "dramatic boom"},
    {"key": "sad_trombone", "keywords": ["fail", "rip"], "mood": "sad", "description": "wah wah wah"},
    {"key": "airhorn", "keywords": ["lets go", "hype"], "mood": "hype", "description": "air horn blast"},
]


def build_bank(directory: Path, rng: random.Random | None = None) -> Soundbank:
    entries = []
    for item in SOUNDS:
        (directory / f"{item['key']}.ogg").write_bytes(b"OggS fake")
        entries.append({**item, "file": f"{item['key']}.ogg", "seconds": 1.5, "waveform": base64.b64encode(bytes(range(0, 255, 4))).decode()})
    (directory / "sounds.json").write_text(json.dumps({"sounds": entries}), encoding="utf-8")
    return Soundbank(directory, rng=rng or random.Random(1))


class SoundbankTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.bank = build_bank(Path(self.tmp.name))

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_loads_only_clips_that_exist(self) -> None:
        (Path(self.tmp.name) / "airhorn.ogg").unlink()
        self.bank.load()

        self.assertEqual(sorted(self.bank.sounds), ["sad_trombone", "vine_boom"])

    def test_lookup_is_forgiving_about_spacing_and_case(self) -> None:
        self.assertEqual(self.bank.get("Vine Boom").key, "vine_boom")
        self.assertEqual(self.bank.get("sad-trombone").key, "sad_trombone")
        self.assertIsNone(self.bank.get("kazoo"))
        self.assertEqual([s.key for s in self.bank.search("hype")], ["airhorn"])

    def test_tags_are_removed_and_the_first_real_sound_is_chosen(self) -> None:
        text, sound = self.bank.extract("no way he did that [sound: kazoo] [SFX: vine boom]")

        self.assertEqual(text, "no way he did that")
        self.assertEqual(sound.key, "vine_boom")

    def test_narrated_sounds_from_weaker_models_count(self) -> None:
        text, sound = self.bank.extract("you failed *sad trombone*")

        self.assertEqual(sound.key, "sad_trombone")
        self.assertEqual(text, "you failed *sad trombone*")  # left as written: it reads fine either way
        self.assertIsNone(self.bank.extract("this is *really* good")[1])

    def test_menus_fit_the_model(self) -> None:
        compact = self.bank.menu(compact=True)
        full = self.bank.menu(compact=False)

        self.assertIn("[sound: name]", compact)
        self.assertIn("vine_boom, sad_trombone, airhorn", compact)
        self.assertIn("vine_boom (dramatic boom)", full)
        self.assertLess(len(compact), len(full))

    def test_tagged_sounds_respect_a_short_cooldown(self) -> None:
        boom = self.bank.get("vine_boom")

        self.assertIs(self.bank.choose(1, boom), boom)
        self.assertIsNone(self.bank.choose(1, boom))
        self.assertIs(self.bank.choose(2, boom), boom)  # per channel

    def test_keyword_matches_are_occasional(self) -> None:
        self.bank.rng = MagicMock(random=MagicMock(return_value=0.9), choice=lambda items: items[0])
        self.assertIsNone(self.bank.choose(1, None, "lets go!!"))

        self.bank.rng.random.return_value = 0.1
        self.assertEqual(self.bank.choose(1, None, "lets go!!").key, "airhorn")
        self.assertIsNone(self.bank.choose(1, None, "rip"))  # long cooldown after any sound
        self.assertIsNone(self.bank.choose(3, None, "nothing relevant here"))

    def test_keywords_match_whole_words_only(self) -> None:
        self.assertIsNone(self.bank.by_keyword("triple threat"))  # "rip" inside a word
        self.assertEqual(self.bank.by_keyword("rip bozo").key, "sad_trombone")


class VoiceMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.sound = build_bank(Path(self.tmp.name)).get("vine_boom")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_attachment_carries_duration_and_waveform(self) -> None:
        file = VoiceMessageFile(self.sound)
        payload = file.to_dict(0)
        file.close()

        self.assertEqual(payload["filename"], "voice-message.ogg")
        self.assertEqual(payload["duration_secs"], 1.5)
        self.assertEqual(base64.b64decode(payload["waveform"]), self.sound.waveform)

    def test_sends_with_the_voice_message_flag(self) -> None:
        http = SimpleNamespace(send_message=AsyncMock(return_value={"id": "1"}))
        target = SimpleNamespace(id=55, _state=SimpleNamespace(http=http, create_message=lambda channel, data: "sent"))
        channel = SimpleNamespace(_get_channel=AsyncMock(return_value=target))

        self.assertEqual(asyncio.run(sounds.send_voice_message(channel, self.sound)), "sent")

        params = http.send_message.await_args.kwargs["params"]
        payload = json.loads(next(part["value"] for part in params.multipart if part["name"] == "payload_json"))
        self.assertEqual(payload["flags"], 8192)
        self.assertEqual(payload["attachments"][0]["duration_secs"], 1.5)

    def test_falls_back_to_an_attachment_when_voice_messages_are_refused(self) -> None:
        import discord

        refused = discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "nope")
        channel = SimpleNamespace(send=AsyncMock(return_value="plain"))

        with patch("psychograph.sounds.send_voice_message", AsyncMock(side_effect=refused)):
            self.assertEqual(asyncio.run(sounds.play(channel, self.sound)), "plain")

        file = channel.send.await_args.kwargs["file"]
        self.assertEqual(file.filename, "vine_boom.ogg")
        file.close()


class PersonaSoundTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.backend = FakeBackend("no way. [sound: vine boom]")
        self.bot = make_bot(self.backend)
        self.bot.soundbank = build_bank(Path(self.tmp.name))
        asyncio.run(self.bot.load_cogs())
        self.me = SimpleNamespace(id=999)
        self.bot._connection.user = self.me
        self.cog = self.bot.get_cog("ChatCog")
        self.discord = FakeDiscord(self.me)

    def tearDown(self) -> None:
        self.bot.store.close()
        self.tmp.cleanup()

    def say(self, text: str, message_id: int):
        message = self.discord.message(text, message_id)
        with patch("psychograph.responder.sounds.play", AsyncMock()) as play:
            asyncio.run(self.cog.on_message(message))
        return message, play

    def test_sounds_stay_off_until_enabled(self) -> None:
        message, play = self.say("<@999> he really did it", 1000)

        self.assertNotIn("[sound: name]", self.backend.calls[0][0]["content"])
        self.assertIn("[sound: vine boom]", message.reply.await_args.kwargs["embed"].description)
        play.assert_not_awaited()

    def test_enabled_personas_get_the_menu_and_their_tags_play(self) -> None:
        self.bot.store.update_channel(1, sounds=True)

        message, play = self.say("<@999> he really did it", 1000)

        self.assertIn("[sound: name]", self.backend.calls[0][0]["content"])
        self.assertEqual(message.reply.await_args.kwargs["embed"].description, "no way.")
        self.assertEqual(play.await_args.args[1].key, "vine_boom")
        self.assertIs(play.await_args.kwargs["reference"], self.discord.posted[0])

    def test_persona_voice_plays_the_clip_as_the_persona(self) -> None:
        posted: list = []
        self.bot.webhooks = fake_webhooks(posted)
        self.bot.store.update_channel(1, sounds=True, persona_voice=True)

        _message, play = self.say("<@999> he really did it", 1000)

        play.assert_not_awaited()
        last_call = self.bot.webhooks.send.await_args
        self.assertEqual(last_call.kwargs["file"].filename, "vine_boom.ogg")
        self.assertEqual(posted[0].content.splitlines()[-1], "no way.")


if __name__ == "__main__":
    unittest.main()
