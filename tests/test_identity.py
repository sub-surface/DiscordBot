import asyncio
import unittest
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from PIL import Image

from psychograph.personas import Persona
from psychograph.webhooks import PersonaWebhooks, member_named, square_png

from .helpers import make_bot


def png(width: int, height: int) -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), (200, 30, 90)).save(output, "PNG")
    return output.getvalue()


def guild_with(*members, cached=None) -> SimpleNamespace:
    return SimpleNamespace(
        id=10,
        get_member_named=lambda name: cached,
        query_members=AsyncMock(return_value=list(members)),
    )


def member(name: str, global_name: str | None = None, nick: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(name=name, global_name=global_name, nick=nick, display_name=nick or global_name or name)


class AvatarImageTests(unittest.TestCase):
    def test_uploads_are_cropped_to_a_square_png(self) -> None:
        result = Image.open(BytesIO(square_png(png(640, 300))))

        self.assertEqual((result.format, result.size), ("PNG", (256, 256)))

    def test_non_images_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            square_png(b"definitely not an image")


class ImpersonationTests(unittest.TestCase):
    def test_member_names_match_exactly_on_any_name_field(self) -> None:
        guild = guild_with(member("leon_the_great", global_name="Leon"), member("santi", nick="Santiago"))

        self.assertTrue(asyncio.run(member_named(guild, "leon")))
        self.assertTrue(asyncio.run(member_named(guild, "SANTIAGO")))
        self.assertFalse(asyncio.run(member_named(guild, "Leo")))
        self.assertFalse(asyncio.run(member_named(guild, "  ")))

    def test_member_search_failures_do_not_block(self) -> None:
        guild = guild_with(cached=member("vostok"))
        guild.query_members.side_effect = asyncio.TimeoutError

        self.assertTrue(asyncio.run(member_named(guild, "vostok")))

    def test_persona_posting_under_a_members_name_is_marked(self) -> None:
        guild = guild_with(member("charlie"))
        webhooks = PersonaWebhooks(SimpleNamespace(user=None))
        channel = SimpleNamespace(guild=guild)

        async def names() -> list[str]:
            return [
                await webhooks.username(channel, Persona("charlie", "charlie", "x")),
                await webhooks.username(channel, Persona("charlie", "charlie", "x")),
                await webhooks.username(channel, Persona("zack", "zack", "x")),
            ]

        guild.query_members.side_effect = lambda query, **_: [member("charlie")] if query == "charlie" else []
        self.assertEqual(asyncio.run(names()), ["charlie (persona)", "charlie (persona)", "zack"])
        self.assertEqual(guild.query_members.call_count, 2)  # cached per name


class PersonaAvatarCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bot = make_bot()
        asyncio.run(self.bot.load_cogs())
        self.cog = self.bot.get_cog("PersonaCommands")
        self.bot.webhooks = MagicMock()
        self.bot.webhooks.available.return_value = True
        self.bot.webhooks.host_avatar = AsyncMock(return_value=("https://cdn.discordapp.com/avatars/1/a.png", 4242))
        self.bot.webhooks.drop_avatar = AsyncMock()

    def tearDown(self) -> None:
        self.bot.store.close()

    def interaction(self, user_id: int, manage_guild: bool) -> SimpleNamespace:
        response = SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), send_modal=AsyncMock(), edit_message=AsyncMock())
        return SimpleNamespace(
            guild=SimpleNamespace(id=10),
            guild_id=10,
            channel=SimpleNamespace(),
            user=SimpleNamespace(id=user_id),
            permissions=SimpleNamespace(manage_guild=manage_guild),
            response=response,
            followup=SimpleNamespace(send=AsyncMock()),
        )

    def run_command(self, name: str, user_id: int, manage_guild: bool, image=None):
        interaction = self.interaction(user_id, manage_guild)
        asyncio.run(self.cog.persona_manage.callback(self.cog, interaction, name, image))
        return interaction

    def attachment(self, data: bytes, content_type: str = "image/png") -> SimpleNamespace:
        return SimpleNamespace(content_type=content_type, size=len(data), read=AsyncMock(return_value=data))

    def test_creator_can_upload_a_custom_personas_picture(self) -> None:
        persona_id = self.bot.store.create_custom_persona(10, 100, "Campfire", "Speak gently.")

        interaction = self.run_command(f"custom:{persona_id}", 100, False, self.attachment(png(300, 300)))

        self.bot.webhooks.host_avatar.assert_awaited_once()
        uploaded = self.bot.webhooks.host_avatar.await_args.args[2]
        self.assertEqual(Image.open(BytesIO(uploaded)).size, (256, 256))
        self.assertEqual(self.bot.store.persona_avatar(10, f"custom:{persona_id}")["webhook_id"], 4242)
        self.assertEqual(self.bot.personas.get(10, f"custom:{persona_id}").avatar_url, "https://cdn.discordapp.com/avatars/1/a.png")
        self.assertIn("New picture saved", interaction.followup.send.await_args.kwargs["embed"].description)

    def test_builtin_pictures_need_manage_server(self) -> None:
        interaction = self.run_command("charlie", 100, False, self.attachment(png(64, 64)))

        self.assertIn("server manager", interaction.response.send_message.await_args.args[0])
        self.bot.webhooks.host_avatar.assert_not_awaited()

        self.run_command("charlie", 100, True, self.attachment(png(64, 64)))
        self.assertIsNotNone(self.bot.store.persona_avatar(10, "charlie"))

    def test_others_cannot_change_a_custom_personas_picture(self) -> None:
        persona_id = self.bot.store.create_custom_persona(10, 100, "Campfire", "Speak gently.")

        self.run_command(f"custom:{persona_id}", 101, False, self.attachment(png(64, 64)))

        self.bot.webhooks.host_avatar.assert_not_awaited()

    def test_rejects_non_images_and_reports_unreadable_files(self) -> None:
        interaction = self.run_command("charlie", 1, True, self.attachment(b"x", "application/pdf"))
        self.assertIn("PNG, JPG", interaction.response.send_message.await_args.args[0])

        interaction = self.run_command("charlie", 1, True, self.attachment(b"not really a png"))
        self.assertIn("isn't an image", interaction.followup.send.await_args.args[0])
        self.assertIsNone(self.bot.store.persona_avatar(10, "charlie"))

    def test_reset_returns_to_the_generated_avatar_and_frees_the_webhook(self) -> None:
        self.run_command("charlie", 1, True, self.attachment(png(64, 64)))

        async def open_panel_and_reset() -> None:
            opened = self.interaction(1, True)
            await self.cog.persona_manage.callback(self.cog, opened, "charlie", None)
            view = opened.response.send_message.await_args.kwargs["view"]
            self.assertNotIn(view.edit_button, view.children)  # built-ins can't be edited
            await view.reset_button.callback(self.interaction(1, True))

        asyncio.run(open_panel_and_reset())

        self.assertIsNone(self.bot.store.persona_avatar(10, "charlie"))
        self.bot.webhooks.drop_avatar.assert_awaited_once_with(4242)
        self.assertIn("dicebear", self.bot.personas.get(10, "charlie").avatar_url)


    def test_manage_panel_offers_only_what_the_person_may_do(self) -> None:
        persona_id = self.bot.store.create_custom_persona(10, 100, "Campfire", "Speak gently.")

        async def panels() -> tuple:
            mine, theirs = self.interaction(100, False), self.interaction(101, False)
            await self.cog.persona_manage.callback(self.cog, mine, f"custom:{persona_id}", None)
            await self.cog.persona_manage.callback(self.cog, theirs, f"custom:{persona_id}", None)
            return mine.response.send_message.await_args.kwargs, theirs.response.send_message.await_args.kwargs

        mine, theirs = asyncio.run(panels())

        self.assertEqual({item.label for item in mine["view"].children}, {"Edit", "Delete"})
        self.assertIsNone(theirs["view"])
        self.assertEqual(mine["embed"].title, "🌱 Campfire")

    def test_new_persona_opens_the_create_form(self) -> None:
        interaction = self.run_command("__new__", 100, False)

        interaction.response.send_modal.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
