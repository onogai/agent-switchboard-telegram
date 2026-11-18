"""Independent Telegram service tests; no Bot API network traffic."""
import tempfile
import io
import json
import urllib.error
from unittest.mock import patch
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from telegram_service.bot_api import BotApi, BotApiError
from telegram_service.service import TelegramDelivery, split_text
from telegram_service.store import DeliveryError, Store


class FakeBot:
    def __init__(self):
        self.calls = []
        self.fail_on = None
        self.pin_error = None
        self.remote_pinned = None

    def call(self, token, method, payload=None):
        self.calls.append((token, method, payload))
        if self.fail_on == method:
            raise BotApiError("telegram_outcome_unknown", uncertain=True)
        if method == "getMe":
            return {"id": 101, "username": "test_bot"}
        if method == "getChat":
            result = {"id": -10012345, "type": "channel"}
            if self.remote_pinned is not None:
                result["pinned_message"] = {"message_id": self.remote_pinned}
            return result
        if method == "getChatMember":
            return {"status": "administrator", "can_pin_messages": True,
                    "can_edit_messages": True}
        if method == "sendMessage":
            return {"message_id": 50 + sum(c[1] == "sendMessage" for c in self.calls)}
        if method == "pinChatMessage" and self.pin_error:
            raise self.pin_error
        if method in {"editMessageText", "pinChatMessage"}:
            return {"message_id": payload["message_id"]}
        raise AssertionError(method)


class TelegramServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "telegram.sqlite3"
        self.bot = FakeBot()
        self._key = Fernet.generate_key().decode()
        self.store = Store(self.path, self._key)
        self.service = TelegramDelivery(self.store, self.bot)
        self.service.register_bot("secret_token", "Main bot")
        self.service.register_destination("news", "101", "-10012345", "News")

    def test_list_tokens_private_and_database_encrypted(self):
        self.assertNotIn("secret_token", self.path.read_bytes().decode("utf-8", "ignore"))
        self.assertNotIn("secret_token", str(self.store.list_bots()))
        self.assertNotIn("secret_token", str(self.store.destinations()))
        self.assertEqual(["news"], [d["slug"] for d in self.store.destinations(True)])

    def test_active_allowlist_and_no_fallback(self):
        with self.assertRaisesRegex(DeliveryError, "Unknown destination"):
            self.service.send("-10099999", "nope")
        self.store.set_destination("news", False)
        with self.assertRaisesRegex(DeliveryError, "disabled"):
            self.service.send("news", "nope")
        self.assertEqual(0, len([c for c in self.bot.calls if c[1] == "sendMessage"]))

    def test_idempotency_success_and_conflict(self):
        first = self.service.send("news", "Hello", "task:1:news")
        second = self.service.send("news", "Hello", "task:1:news")
        self.assertEqual("accepted", first["status"])
        self.assertEqual(first, second)
        self.assertEqual(1, len([c for c in self.bot.calls if c[1] == "sendMessage"]))
        with self.assertRaisesRegex(DeliveryError, "Idempotency"):
            self.service.send("news", "Different", "task:1:news")

    def test_timeout_unknown_is_not_retried(self):
        self.bot.fail_on = "sendMessage"
        first = self.service.send("news", "Hello", "task:2:news")
        self.assertEqual("unknown", first["status"])
        self.assertEqual(first, self.service.send("news", "Hello", "task:2:news"))
        self.assertEqual(1, len([c for c in self.bot.calls if c[1] == "sendMessage"]))

    def test_long_unicode_text_split_without_loss(self):
        text = "😀" * 2300
        parts = split_text(text)
        self.assertEqual(text, "".join(parts))
        self.assertEqual(2, len(parts))
        delivery = self.service.send("news", text)
        self.assertEqual("accepted", delivery["status"])
        self.assertEqual(2, delivery["parts_sent"])

    def test_pinned_summary_updates_existing_message(self):
        one = self.service.summary("news", "daily:1", "First")
        two = self.service.summary("news", "daily:1", "Updated")
        three = self.service.summary("news", "daily:1", "Updated")
        self.assertEqual("accepted", one["status"])
        self.assertEqual(one["message_id"], two["message_id"])
        self.assertTrue(three["unchanged"])
        self.assertEqual(1, len([c for c in self.bot.calls if c[1] == "sendMessage"]))
        self.assertEqual(1, len([c for c in self.bot.calls if c[1] == "pinChatMessage"]))

    def test_summary_pin_rejection_does_not_resend(self):
        self.bot.fail_on = "pinChatMessage"
        first = self.service.summary("news", "daily:2", "Summary")
        self.assertEqual("partial", first["status"])
        self.assertTrue(first["content_saved"])
        self.bot.fail_on = None
        second = self.service.summary("news", "daily:2", "Summary")
        self.assertEqual("accepted", second["status"])
        self.assertEqual(1, len([c for c in self.bot.calls if c[1] == "sendMessage"]))

    def test_idempotency_survives_process_restart(self):
        result = self.service.send("news", "Hello", "stable-key")
        other = TelegramDelivery(self.store, self.bot)
        self.assertEqual(result, other.send("news", "Hello", "stable-key"))
        self.assertEqual(1, len([c for c in self.bot.calls if c[1] == "sendMessage"]))

    def test_mutated_bot_fails_closed(self):
        self.store.set_bot("101", False)
        self.assertFalse(self.store.destinations(True))
        with self.assertRaisesRegex(DeliveryError, "disabled"):
            self.service.send("news", "message")

    def test_rejected_message_does_not_fallback(self):
        self.bot.fail_on = "sendMessage"
        result = self.service.send("news", "Rejected")
        self.assertEqual("unknown", result["status"])
        self.assertEqual(0, result["parts_sent"])

    def test_abandoned_attempt_is_not_resubmitted(self):
        record, first = self.store.begin("news", "send", {"text": "Hello"}, "crash-key")
        self.assertTrue(first)
        outcome = self.service.send("news", "Hello", "crash-key")
        self.assertEqual(record["delivery_id"], outcome["delivery_id"])
        self.assertEqual("unknown", outcome["status"])
        self.assertEqual(0, len([c for c in self.bot.calls if c[1] == "sendMessage"]))

    def test_pin_400_preserves_content_and_retry_reuses_message(self):
        self.bot.pin_error = BotApiError("telegram_http_400", error_code=400,
                                         description="Bad Request: not enough rights to pin")
        first = self.service.summary("news", "news:2026-11-15", "Daily summary", True)
        self.assertEqual("partial", first["status"])
        self.assertTrue(first["content_saved"])
        self.assertEqual("failed", first["pin_status"])
        self.assertEqual(400, first["error_code"])
        self.assertIn("not enough rights", first["description"])
        self.assertEqual(str(first["message_id"]),
                         self.store.summary("news", "news:2026-11-15")["message_id"])
        self.bot.pin_error = None
        retried = self.service.pin_summary("news", "news:2026-11-15")
        self.assertEqual("accepted", retried["status"])
        self.assertEqual(first["message_id"], retried["message_id"])
        self.assertEqual(1, len([x for x in self.bot.calls if x[1] == "sendMessage"]))
        self.assertEqual(0, len([x for x in self.bot.calls if x[1] == "editMessageText"]))
        pins = [x for x in self.bot.calls if x[1] == "pinChatMessage"]
        self.assertEqual(2, len(pins))
        self.assertTrue(pins[-1][2]["disable_notification"])
        self.service.pin_summary("news", "news:2026-11-15")
        self.assertEqual(2, len([x for x in self.bot.calls if x[1] == "pinChatMessage"]))

    def test_edit_pin_failure_keeps_hash_and_avoids_repeat_edit(self):
        self.service.summary("news", "daily:edited", "Original", pin=False)
        self.bot.pin_error = BotApiError("telegram_http_400", error_code=400,
                                         description="Bad Request: missing rights")
        result = self.service.summary("news", "daily:edited", "Edited", pin=True)
        self.assertEqual("partial", result["status"])
        self.assertEqual(1, len([x for x in self.bot.calls if x[1] == "editMessageText"]))
        self.service.summary("news", "daily:edited", "Edited", pin=True)
        self.assertEqual(1, len([x for x in self.bot.calls if x[1] == "editMessageText"]))
        self.assertEqual(1, len([x for x in self.bot.calls if x[1] == "sendMessage"]))

    def test_remote_pinned_message_skips_repin(self):
        first = self.service.summary("news", "daily:remote", "Summary", pin=False)
        self.bot.remote_pinned = int(first["message_id"])
        result = self.service.pin_summary("news", "daily:remote")
        self.assertEqual("accepted", result["status"])
        self.assertTrue(result["verified"])
        self.assertFalse([x for x in self.bot.calls if x[1] == "pinChatMessage"])

    def test_pin_only_rejects_unknown_key(self):
        with self.assertRaisesRegex(DeliveryError, "Unknown summary"):
            self.service.pin_summary("news", "unknown")
        self.assertFalse([x for x in self.bot.calls if x[1] == "sendMessage"])

    def test_pin_permissions_by_chat_type(self):
        self.assertEqual("can_edit_messages", self.service.pin_capability("news")["required_right"])
        with self.store.connect() as db:
            db.execute("UPDATE destinations SET chat_type='group' WHERE slug='news'")
        self.assertEqual("can_pin_messages", self.service.pin_capability("news")["required_right"])

    def test_bot_api_400_preserves_safe_description(self):
        token = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef123456"
        error = urllib.error.HTTPError(
            "https://api.telegram.org/bot" + token + "/pinChatMessage",
            400, "Bad Request", None, io.BytesIO(json.dumps({
                "ok": False, "error_code": 400,
                "description": "Bad Request: not enough rights " + token}).encode()))
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(BotApiError) as ctx:
                BotApi().call(token, "pinChatMessage", {"chat_id": -10012345})
        self.assertEqual(400, ctx.exception.error_code)
        self.assertIn("not enough rights", ctx.exception.description)
        self.assertNotIn(token, ctx.exception.description)
        self.assertNotIn(token, str(ctx.exception))

    def test_existing_sqlite_migrates_diagnostic_columns(self):
        with self.store.connect() as db:
            db.execute("DROP TABLE deliveries")
            db.execute("""CREATE TABLE deliveries (
                id TEXT PRIMARY KEY, destination TEXT NOT NULL, operation TEXT NOT NULL,
                idempotency_key TEXT UNIQUE, request_hash TEXT NOT NULL,
                status TEXT NOT NULL, message_ids TEXT NOT NULL DEFAULT '[]',
                parts_sent INTEGER NOT NULL DEFAULT 0,
                error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        restored = Store(self.path, self._key)
        with restored.connect() as db:
            columns = {row["name"] for row in db.execute("PRAGMA table_info(deliveries)")}
        self.assertIn("error_description", columns)
        self.assertIn("telegram_error_code", columns)


if __name__ == "__main__":
    unittest.main()
