import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch, PropertyMock
import os
import sys

# Add current dir to sys.path
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import db
import bot
import personas
import llm

class TestSimCity(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Initialize an in-memory DB for testing
        db._conn = db.sqlite3.connect(":memory:")
        db._conn.row_factory = db.sqlite3.Row
        db.init_db()
        # Mock bot.user to avoid regex issues
        bot.bot.user = MagicMock()
        bot.bot.user.id = 12345
        self.bot_instance = bot.bot

    def test_db_webhook_persistence(self):
        """Test that we can save and get webhooks from the DB."""
        db.save_channel_webhook(123, "http://webhook.url", 456)
        webhook = db.get_channel_webhook(123)
        self.assertIsNotNone(webhook)
        self.assertEqual(webhook['webhook_url'], "http://webhook.url")
        self.assertEqual(webhook['webhook_id'], 456)

    def test_db_heartbeat_persistence(self):
        """Test that we can save and get last run times from the DB."""
        db.set_last_run("test_task", 1000.0)
        last_run = db.get_last_run("test_task")
        self.assertEqual(last_run, 1000.0)

    @patch("personas.list_personas")
    @patch("db.get_channel_persona")
    @patch("bot.get_system_prompt")
    @patch("llm.format_image_blocks")
    async def test_on_message_persona_detection(self, mock_img_blocks, mock_sys_prompt, mock_get_ch_persona, mock_list_personas):
        """Test that on_message correctly identifies [PersonaName] in #sim-city."""
        mock_list_personas.return_value = ["Mochi", "The Merchant", "Sigint Ghost"]
        mock_get_ch_persona.return_value = "Mochi"
        mock_sys_prompt.return_value = "System prompt"
        mock_img_blocks.return_value = []
        
        # Mock message in #sim-city
        message = MagicMock()
        message.author.bot = False
        message.channel.name = "sim-city"
        message.channel.id = 123
        message.content = "[Mochi] Hello there!"
        message.mentions = []
        message.reference = None
        message.attachments = []
        message.webhook_id = None

        # We mock process_llm_request to see what persona it gets
        with patch("bot.process_llm_request", new_callable=AsyncMock) as mock_process:
            await bot.on_message(message)
            
            # Verify it detected "Mochi"
            mock_process.assert_called()
            args, kwargs = mock_process.call_args
            # Args: channel, messages, persona, parent_msg_id
            self.assertEqual(args[2], "Mochi")
            
            # Check prompt was stripped of the [Mochi] tag in the last user message
            last_msg = args[1][-1]
            self.assertIn("Hello there!", last_msg['content'])

    @patch("bot.bot.get_or_create_webhook")
    @patch("llm.complete")
    async def test_webhook_use_in_sim_city(self, mock_llm_complete, mock_get_webhook):
        """Test that process_llm_request uses a webhook if in #sim-city."""
        mock_webhook = AsyncMock()
        mock_get_webhook.return_value = mock_webhook
        # Mocking the response from webhook.send
        sent_msg = MagicMock()
        sent_msg.id = 999
        mock_webhook.send.return_value = sent_msg
        
        # Mock LLM generator
        async def mock_gen(*args, **kwargs):
            yield "Hello!", None
            yield None, {"prompt_tokens": 10, "completion_tokens": 10, "duration": 1.0, "model": "test-model", "provider": "test"}
        mock_llm_complete.side_effect = mock_gen
        
        channel = MagicMock()
        channel.name = "sim-city"
        channel.id = 123
        
        messages = [{"role": "user", "content": "Hi"}]
        
        await bot.process_llm_request(channel, messages, "Mochi", 789)
        
        # Verify it tried to use the webhook
        mock_webhook.send.assert_called()

    @patch("bot.PsychographBot.guilds", new_callable=PropertyMock)
    @patch("bot.process_llm_request", new_callable=AsyncMock)
    @patch("db.get_last_run")
    @patch("db.set_last_run")
    @patch("personas.list_personas")
    async def test_heartbeat_trigger(self, mock_list_personas, mock_set_last_run, mock_get_last_run, mock_process, mock_guilds_prop):
        """Test that heartbeat triggers correctly when time has passed."""
        # Mock that it hasn't run recently
        mock_get_last_run.return_value = 0.0
        mock_list_personas.return_value = ["normal_dude"]
        
        # Mock a guild and a channel named #sim-city
        mock_guild = MagicMock()
        mock_channel = MagicMock()
        mock_channel.name = "sim-city"
        mock_channel.id = 123
        mock_guild.text_channels = [mock_channel]
        mock_guilds_prop.return_value = [mock_guild]
        
        # Force the heartbeat loop to run once
        await self.bot_instance.heartbeat()
        
        # Verify it triggered an LLM request
        mock_process.assert_called()
        mock_set_last_run.assert_called_with("sim_city_heartbeat", unittest.mock.ANY)

if __name__ == "__main__":
    unittest.main()
