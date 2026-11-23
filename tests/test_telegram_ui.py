"""Host-neutral Telegram panel smoke tests."""
import os
import unittest
from unittest.mock import patch
import httpx
from telegram_service.app import app


class TelegramUiTests(unittest.IsolatedAsyncioTestCase):
    async def test_ui_requires_admin_token(self):
        with patch.dict(os.environ, {"TELEGRAM_ADMIN_TOKEN": "x" * 48}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://example.test") as client:
                self.assertEqual(401, (await client.get("/internal/ui/")).status_code)
                response = await client.get("/internal/ui/",
                    headers={"Authorization": "Bearer " + "x" * 48})
                self.assertEqual(200, response.status_code)
                self.assertIn("Telegram Delivery", response.text)
                self.assertIn("Chats &amp; routes", response.text)
                self.assertNotIn("x" * 48, response.text)

    async def test_ui_api_alias_is_private(self):
        with patch.dict(os.environ, {"TELEGRAM_ADMIN_TOKEN": "x" * 48}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://example.test") as client:
                self.assertEqual(401, (await client.get("/internal/ui/api/bots")).status_code)
