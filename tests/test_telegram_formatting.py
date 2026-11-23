"""Telegram HTML delivery regression tests (no real network calls)."""
import asyncio
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from telegram_service.formatting import split_html, format_parts, units
from telegram_service.service import TelegramDelivery
from telegram_service.store import Store, DeliveryError
from telegram_service.app import telegram_send, telegram_upsert_summary
import telegram_service.app as app_module


class FakeBot:
    def __init__(self):
        self.calls = []

    def call(self, token, method, payload=None):
        self.calls.append((method, payload))
        if method == "getMe":
            return {"id": 101, "username": "test_bot"}
        if method == "getChat":
            return {"id": -10012345, "type": "channel"}
        if method == "sendMessage":
            return {"message_id": 100 + len([call for call in self.calls if call[0] == "sendMessage"])}
        if method in {"editMessageText", "pinChatMessage"}:
            return {"message_id": payload["message_id"]}
        raise AssertionError(method)


class FormattingTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bot = FakeBot()
        self.store = Store(Path(tmp.name) / "telegram.sqlite3", Fernet.generate_key().decode())
        self.service = TelegramDelivery(self.store, self.bot)
        self.service.register_bot("secret_token", "Main")
        self.service.register_destination("news", "101", "-10012345", "News")

    def calls(self, method):
        return [c[1] for c in self.bot.calls if c[0] == method]

    def test_default_plain_text_unchanged(self):
        self.service.send("news", "<b>Literal</b>")
        self.assertEqual({"chat_id": "-10012345", "text": "<b>Literal</b>"},
                         self.calls("sendMessage")[-1])

    def test_html_send_and_idempotency_include_format(self):
        text = "<b>Market</b> &amp; <a href=\"https://example.com\">stocks</a>"
        a = self.service.send("news", text, "same-key", "HTML")
        b = self.service.send("news", text, "same-key", "HTML")
        self.assertEqual(a, b)
        self.assertEqual(1, len(self.calls("sendMessage")))
        self.assertEqual("HTML", self.calls("sendMessage")[0]["parse_mode"])
        with self.assertRaisesRegex(DeliveryError, "Idempotency"):
            self.service.send("news", text, "same-key")

    def test_long_html_keeps_valid_balanced_tags(self):
        text = '<b><a href="https://example.com"> ' + "😀" * 2500 + "</a></b>"
        parts = split_html(text)
        self.assertEqual(2, len(parts))
        self.assertTrue(all(p.startswith("<b><a") and p.endswith("</a></b>") for p in parts))
        self.assertTrue(all(units("😀" * (p.count("😀"))) <= 4096 for p in parts))
        sent = self.service.send("news", text, parse_mode="HTML")
        self.assertEqual("accepted", sent["status"])
        self.assertEqual(2, sent["parts_sent"])
        self.assertEqual(2, len(self.calls("sendMessage")))

    def test_html_escaping_entities_unicode_and_reopen(self):
        parts = split_html("<b>" + "😀" * 2048 + "&amp; second</b>")
        self.assertEqual(2, len(parts))
        self.assertIn("&amp;", parts[1])
        self.assertTrue(parts[1].startswith("<b>"))
        self.assertTrue(parts[0].endswith("</b>"))

    def test_reject_malformed_and_unsupported_modes_before_sending(self):
        for bad, mode in [
            ("<b>unclosed", "HTML"),
            ("<script>alert(1)</script>", "HTML"),
            ("<b><i>x</b></i>", "HTML"),
            ("<b>Valid</b>", "MarkdownV2"),
            ("<b></b>", "HTML"),
        ]:
            with self.subTest(bad=bad, mode=mode):
                with self.assertRaises(DeliveryError):
                    self.service.send("news", bad, parse_mode=mode)
        self.assertEqual([], self.calls("sendMessage"))

    def test_summary_format_change_edits_existing_message(self):
        a = self.service.summary("news", "daily", "<b>Market</b>", False)
        b = self.service.summary("news", "daily", "<b>Market</b>", False, "HTML")
        c = self.service.summary("news", "daily", "<b>Market</b>", False, "HTML")
        self.assertEqual(a["message_id"], b["message_id"])
        self.assertFalse(b["unchanged"])
        self.assertTrue(c["unchanged"])
        self.assertEqual("HTML", self.calls("editMessageText")[0]["parse_mode"])
        self.assertEqual(1, len(self.calls("sendMessage")))
        self.assertEqual(1, len(self.calls("editMessageText")))

    def test_summary_rendered_length_guard(self):
        with self.assertRaises(DeliveryError):
            self.service.summary("news", "too-long", "<b>" + "😀" * 2049 + "</b>", parse_mode="HTML")
        self.assertFalse(self.calls("sendMessage"))

    def test_mcp_contract_accepts_html(self):
        original = app_module._service
        app_module._service = self.service
        try:
            a = asyncio.run(telegram_send("news", "<b>Hello</b>", parse_mode="HTML"))
            b = asyncio.run(telegram_upsert_summary("news", "daily", "<i>News</i>",
                                                     pin=False, parse_mode="HTML"))
        finally:
            app_module._service = original
        self.assertEqual("accepted", a["status"])
        self.assertEqual("accepted", b["status"])
        self.assertEqual("HTML", self.calls("sendMessage")[-1]["parse_mode"])


if __name__ == "__main__":
    unittest.main()
