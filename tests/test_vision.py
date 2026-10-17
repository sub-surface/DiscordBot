import asyncio
import base64
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
from PIL import Image

from psychograph import conversation, vision

from .helpers import FakeBackend, FakeDiscord, make_bot


def attachment(filename: str, content_type: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(filename=filename, content_type=content_type, url=f"https://cdn.discordapp.com/attachments/1/2/{filename}?ex=1")


class ImageUrlTests(unittest.TestCase):
    def test_collects_image_attachments_and_proxied_picture_embeds_in_order(self) -> None:
        linked = discord.Embed.from_dict({"type": "image", "thumbnail": {
            "url": "http://192.168.1.1/cat.png", "proxy_url": "https://images-ext-1.discordapp.net/external/x/cat.png",
        }})
        preview = discord.Embed.from_dict({"type": "link", "thumbnail": {  # a link preview's thumbnail isn't the point
            "url": "https://news.example/thumb.jpg", "proxy_url": "https://images-ext-2.discordapp.net/external/y/thumb.jpg",
        }})
        message = SimpleNamespace(
            attachments=[attachment("a.png", "image/png"), attachment("notes.txt", "text/plain"), attachment("b.JPG")],
            embeds=[linked, preview],
        )
        replied = SimpleNamespace(attachments=[attachment("c.webp", "image/webp")], embeds=[])

        urls = vision.image_urls(message, None, replied)

        self.assertEqual(urls, [
            "https://cdn.discordapp.com/attachments/1/2/a.png?ex=1", "https://cdn.discordapp.com/attachments/1/2/b.JPG?ex=1",
            "https://images-ext-1.discordapp.net/external/x/cat.png",
        ])  # the proxied copy, never the poster's URL; capped at MAX_IMAGES, so the replied-to picture is dropped

    def test_only_discord_media_hosts_are_ever_fetched(self) -> None:
        stray = SimpleNamespace(attachments=[SimpleNamespace(filename="x.png", content_type="image/png", url="http://169.254.169.254/x.png")], embeds=[])
        self.assertEqual(vision.image_urls(stray), [])
        session = SimpleNamespace(get=AsyncMock())
        self.assertIsNone(asyncio.run(vision._fetch(session, "https://localhost/x.png")))
        session.get.assert_not_called()

    def test_shrink_fits_the_longest_side_and_flattens_to_jpeg(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGBA", (3000, 1500), (255, 0, 0, 128)).save(buffer, format="PNG")

        data_url = vision._shrink(buffer.getvalue())

        prefix = "data:image/jpeg;base64,"
        self.assertTrue(data_url.startswith(prefix))
        with Image.open(io.BytesIO(base64.b64decode(data_url[len(prefix):]))) as image:
            self.assertEqual((image.format, image.size), ("JPEG", (vision.MAX_SIDE, vision.MAX_SIDE // 2)))

    def test_a_download_arriving_in_chunks_is_read_whole(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGB", (300, 200), (0, 128, 255)).save(buffer, format="PNG")
        data = buffer.getvalue()

        class Response:
            status, content_length = 200, len(data)
            content = SimpleNamespace(iter_chunked=lambda size: self._chunks(data, 100))

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

        session = SimpleNamespace(get=lambda url, allow_redirects: Response())
        data_url = asyncio.run(vision._fetch(session, "https://cdn.discordapp.com/attachments/1/2/a.png"))

        self.assertTrue(data_url and data_url.startswith("data:image/jpeg;base64,"))

    @staticmethod
    async def _chunks(data: bytes, size: int):
        for start in range(0, len(data), size):
            yield data[start:start + size]

    def test_attach_and_token_estimate_count_images_not_base64(self) -> None:
        message = {"role": "user", "content": "Leon: what is this"}
        vision.attach(message, ["data:image/jpeg;base64," + "A" * 50_000])

        self.assertEqual(message["content"][0], {"type": "text", "text": "Leon: what is this"})
        self.assertEqual(message["content"][1]["type"], "image_url")
        tokens = conversation.estimate_tokens([message])
        self.assertLess(tokens, vision.IMAGE_TOKENS + 50)
        self.assertGreaterEqual(tokens, vision.IMAGE_TOKENS)


class TweetPhotoTests(unittest.TestCase):
    def tearDown(self) -> None:
        conversation._STATUS_CACHE.clear()

    def test_public_record_gives_photos_and_video_thumbnails_and_is_fetched_once(self) -> None:
        body = (b'{"code":200,"status":{"text":"t","media":{"photos":[{"url":"https://pbs.twimg.com/media/a.jpg"}],'
                b'"videos":[{"thumbnail_url":"https://pbs.twimg.com/ext_tw_video_thumb/b.jpg"}]}}}')
        with patch("psychograph.conversation.urlopen", return_value=io.BytesIO(body)) as open_url:
            self.assertEqual(conversation.fetch_public_tweet("9"), "t")
            self.assertEqual(conversation.public_tweet_photos("9"), [
                "https://pbs.twimg.com/media/a.jpg", "https://pbs.twimg.com/ext_tw_video_thumb/b.jpg",
            ])
        open_url.assert_called_once()

    def test_a_preview_that_carries_the_photo_skips_the_lookup(self) -> None:
        preview = discord.Embed.from_dict({"url": "https://x.com/alice/status/123", "image": {
            "url": "https://pbs.twimg.com/media/a.jpg", "proxy_url": "https://media.discordapp.net/external/a.jpg",
        }})
        with patch.object(conversation, "public_tweet_photos") as photos:
            self.assertEqual(asyncio.run(conversation.tweet_photos("https://x.com/alice/status/123", [preview])), [])
        photos.assert_not_called()

    def test_twitter_media_is_fetchable_but_its_links_are_not_collected_from_messages(self) -> None:
        self.assertTrue(vision._hosted("https://pbs.twimg.com/media/a.jpg", vision.MEDIA_HOSTS))
        self.assertFalse(vision._hosted("http://pbs.twimg.com/media/a.jpg", vision.MEDIA_HOSTS))
        stray = SimpleNamespace(attachments=[attachment("x.png", "image/png")], embeds=[])
        stray.attachments[0].url = "https://pbs.twimg.com/media/x.png"
        self.assertEqual(vision.image_urls(stray), [])


class VisionPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = FakeBackend("Nice cat.")
        self.bot = make_bot(self.backend)
        asyncio.run(self.bot.load_cogs())
        self.me = SimpleNamespace(id=999)
        self.bot._connection.user = self.me
        self.cog = self.bot.get_cog("ChatCog")
        self.discord = FakeDiscord(self.me)

    def tearDown(self) -> None:
        self.bot.store.close()

    def say(self, content: str, *attachments: SimpleNamespace) -> None:
        message = self.discord.message(content, 1000)
        message.attachments = list(attachments)
        asyncio.run(self.cog.on_message(message))

    def test_a_vision_model_sees_the_posted_image(self) -> None:
        self.backend.vision = True
        with patch.object(vision, "inline", AsyncMock(return_value=["data:image/jpeg;base64,AAAA"])) as inline:
            self.say("<@999> rate my cat", attachment("cat.png", "image/png"))

        inline.assert_awaited_once_with(["https://cdn.discordapp.com/attachments/1/2/cat.png?ex=1"])
        text, image = self.backend.calls[0][-1]["content"]
        self.assertTrue(text["text"].startswith("Leon: rate my cat"))
        self.assertIn("(1 image shown with this message.)", text["text"])
        self.assertEqual(image, {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,AAAA"}})
        stored = self.bot.store.message_chain(2000, 1)[0]["content"]
        self.assertIn("1 image shown", stored)
        self.assertNotIn("base64", stored)

    def test_an_image_alone_is_enough_to_answer(self) -> None:
        self.backend.vision = True
        with patch.object(vision, "inline", AsyncMock(return_value=["data:image/jpeg;base64,AAAA"])):
            self.say("<@999>", attachment("meme.gif", "image/gif"))

        self.assertIn("just the image", self.backend.calls[0][-1]["content"][0]["text"])

    def test_a_linked_post_without_a_preview_brings_its_photos(self) -> None:
        self.backend.vision = True
        photo = "https://pbs.twimg.com/media/abc.jpg"
        with patch.object(conversation, "fetch_public_tweet", return_value="Alice: look"),                 patch.object(conversation, "public_tweet_photos", return_value=[photo]) as photos,                 patch.object(vision, "inline", AsyncMock(return_value=["data:image/jpeg;base64,AAAA"])) as inline:
            self.say("<@999> is this real https://x.com/alice/status/123")

        photos.assert_called_once_with("123")
        inline.assert_awaited_once_with([photo])
        self.assertIn("(1 image (from the linked post) shown with this message.)", self.backend.calls[0][-1]["content"][0]["text"])

    def test_a_text_model_never_downloads_images(self) -> None:
        with patch.object(vision, "inline", AsyncMock()) as inline:
            self.say("<@999> rate my cat", attachment("cat.png", "image/png"))

        inline.assert_not_awaited()
        self.assertEqual(self.backend.calls[0][-1]["content"], "Leon: rate my cat")


if __name__ == "__main__":
    unittest.main()
