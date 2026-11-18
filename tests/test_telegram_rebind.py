"""Destination generation regression tests; no real Telegram network calls."""
import tempfile
import sqlite3
import hashlib
import json
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from telegram_service.service import TelegramDelivery
from telegram_service.store import Store, DeliveryError


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.next_message = 700

    def call(self, token, method, payload=None):
        self.calls.append((method, payload))
        if method == "getMe":
            return {"id": 101, "username": "bot"}
        if method == "getChat":
            chat_id = int(payload["chat_id"])
            return {"id": chat_id, "type": "supergroup" if chat_id == -10098765 else "group"}
        if method == "sendMessage":
            self.next_message += 1
            return {"message_id": self.next_message}
        if method in ("pinChatMessage", "editMessageText"):
            return True
        raise AssertionError(method)


class DestinationRebindTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "hub.sqlite3"
        self.key = Fernet.generate_key().decode()
        self.store = Store(self.path, self.key)
        self.telegram = FakeTelegram()
        self.hub = TelegramDelivery(self.store, self.telegram)
        self.hub.register_bot("dummy", "Bot")
        self.hub.register_destination("news", "101", "-10012345", "News")
        self.old = self.store.get_destination("news")

    def discover_new(self):
        self.store.record_discovery_update("101", 18, {
            "chat_id": "-10098765", "chat_type": "supergroup",
            "title": "News SG", "source": "membership", "present": True})

    def test_rebind_preserves_old_deliveries_and_summaries(self):
        historic = self.hub.send("news", "Digest", "run:2026-11-15")
        summary = self.hub.summary("news", "news:2026-11-15", "Old summary", pin=True)
        self.assertEqual("accepted", summary["status"])
        self.discover_new()
        moved = self.hub.rebind_destination("news", self.old["binding_id"],
                                             "101", "-10098765", "News")
        self.assertEqual("supergroup", moved["chat_type"])
        self.assertNotEqual(self.old["binding_id"], moved["binding_id"])
        self.assertIsNone(self.store.summary("news", "news:2026-11-15"))
        with self.store.connect() as db:
            archived = db.execute("SELECT * FROM archived_summaries WHERE binding_id=?",
                                  (self.old["binding_id"],)).fetchone()
            record = db.execute("SELECT * FROM deliveries WHERE id=?",
                                (historic["delivery_id"],)).fetchone()
        self.assertEqual(summary["message_id"], archived["message_id"])
        self.assertEqual("-10012345", archived["chat_id"])
        self.assertEqual("-10012345", record["chat_id"])
        self.assertEqual(self.old["binding_id"], record["binding_id"])

        # An identical logical key must NOT reuse the old physical delivery.
        new = self.hub.send("news", "Digest", "run:2026-11-15")
        self.assertEqual("accepted", new["status"])
        self.assertNotEqual(new["delivery_id"], historic["delivery_id"])
        self.assertEqual(moved["binding_id"], new["binding_id"])
        self.assertEqual("-10098765", new["chat_id"])
        self.assertEqual("run:2026-11-15", new["logical_idempotency_key"])
        self.assertEqual("-10098765", self.telegram.calls[-1][1]["chat_id"])
        fresh = self.hub.summary("news", "news:2026-11-15", "New summary", pin=True)
        self.assertEqual("accepted", fresh["status"])
        self.assertNotEqual(summary["message_id"], fresh["message_id"])
        self.assertEqual("-10098765", [payload["chat_id"] for name, payload
                                      in self.telegram.calls if name == "pinChatMessage"][-1])
        self.assertEqual(summary["message_id"], archived["message_id"])

    def test_delete_frees_slug_preserves_history_and_reuse_is_isolated(self):
        delivered = self.hub.send("news", "Old", "same-key")
        self.hub.summary("news", "summary-key", "Old content", pin=False)
        outcome = self.hub.delete_destination("news", self.old["binding_id"])
        self.assertEqual(1, outcome["archived_summaries"])
        with self.assertRaisesRegex(DeliveryError, "Unknown destination"):
            self.hub.send("news", "Do not send")
        self.hub.register_destination("news", "101", "-10098765", "News")
        again = self.hub.send("news", "Old", "same-key")
        self.assertNotEqual(delivered["delivery_id"], again["delivery_id"])
        self.assertNotEqual(delivered["binding_id"], again["binding_id"])
        self.assertEqual("-10098765", again["chat_id"])
        self.assertIsNone(self.store.summary("news", "summary-key"))
        self.assertIn(delivered["delivery_id"],
                      [d["delivery_id"] for d in self.store.recent(20)])

    def test_rebind_rejects_unapproved_target_and_stale_review(self):
        with self.assertRaisesRegex(DeliveryError, "discovered"):
            self.hub.rebind_destination("news", self.old["binding_id"],
                                        "101", "-10098765", "News")
        self.discover_new()
        self.hub.rebind_destination("news", self.old["binding_id"],
                                    "101", "-10098765", "News")
        with self.assertRaisesRegex(DeliveryError, "changed since review"):
            self.hub.delete_destination("news", self.old["binding_id"])

    def test_cannot_bind_chat_used_by_another_destination(self):
        self.discover_new()
        self.hub.register_destination("other", "101", "-10098765", "Other")
        with self.assertRaisesRegex(DeliveryError, "already belongs"):
            self.hub.rebind_destination("news", self.old["binding_id"],
                                        "101", "-10098765", "News")
        self.assertEqual(self.old["binding_id"],
                         self.store.get_destination("news")["binding_id"])

    def test_store_reopening_does_not_renamespace_idempotency(self):
        old = self.hub.send("news", "Original", "stable-key")
        restored = Store(self.path, self.key)
        same = TelegramDelivery(restored, self.telegram).send("news", "Original", "stable-key")
        self.assertEqual(old["delivery_id"], same["delivery_id"])
        self.assertEqual(1, len([name for name, payload in self.telegram.calls
                                 if name == "sendMessage"]))

    def test_migration_from_pre_binding_schema_preserves_summary_and_delivery(self):
        legacy = Path(self.tmp.name) / "legacy.sqlite3"
        with sqlite3.connect(legacy) as db:
            db.executescript("""
                PRAGMA foreign_keys=ON;
                CREATE TABLE bots(id TEXT PRIMARY KEY,username TEXT NOT NULL,label TEXT NOT NULL,
                    encrypted_token TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL);
                CREATE TABLE destinations(slug TEXT PRIMARY KEY,bot_id TEXT NOT NULL REFERENCES bots(id),
                    chat_id TEXT NOT NULL,label TEXT NOT NULL,chat_type TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,
                    UNIQUE(bot_id,chat_id));
                CREATE TABLE deliveries(id TEXT PRIMARY KEY,destination TEXT NOT NULL,
                    operation TEXT NOT NULL,idempotency_key TEXT UNIQUE,request_hash TEXT NOT NULL,
                    status TEXT NOT NULL,message_ids TEXT NOT NULL DEFAULT '[]',
                    parts_sent INTEGER NOT NULL DEFAULT 0,error_code TEXT,
                    telegram_error_code INTEGER,error_description TEXT,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
                CREATE TABLE summaries(destination TEXT NOT NULL REFERENCES destinations(slug),
                    summary_key TEXT NOT NULL,message_id TEXT NOT NULL,content_hash TEXT NOT NULL,
                    pinned INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,
                    PRIMARY KEY(destination,summary_key));
            """)
            token = Fernet(self.key.encode()).encrypt(b"dummy").decode()
            db.execute("INSERT INTO bots VALUES(?,?,?,?,?,?)",
                       ("101", "bot", "Bot", token, 1, "2026-11-15"))
            db.execute("INSERT INTO destinations VALUES(?,?,?,?,?,?,?)",
                       ("news", "101", "-10012345", "News", "group", 1, "2026-11-15"))
            fingerprint = hashlib.sha256(json.dumps(
                ["send", "news", {"text": "Old"}],
                sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            db.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       ("legacy-delivery", "news", "send", "stable-key", fingerprint,
                        "accepted", '["799"]', 1, None, None, None,
                        "2026-11-15", "2026-11-15"))
            db.execute("INSERT INTO summaries VALUES(?,?,?,?,?,?)",
                       ("news", "news:2026-11-15", "800", "digest", 0, "2026-11-15"))

        restored = Store(legacy, self.key)
        route = restored.get_destination("news")
        archived = restored.summary("news", "news:2026-11-15")
        self.assertEqual("800", archived["message_id"])
        self.assertEqual("legacy-delivery", restored.recent(10)[0]["delivery_id"])
        self.assertEqual(route["binding_id"], restored.recent(10)[0]["binding_id"])
        self.assertEqual("-10012345", restored.recent(10)[0]["chat_id"])
        self.assertEqual("stable-key", restored.recent(10)[0]["logical_idempotency_key"])
        # Reopening cannot change the legacy binding or remap idempotency.
        again = Store(legacy, self.key)
        self.assertEqual(route["binding_id"],
                         again.get_destination("news")["binding_id"])
        old_result = TelegramDelivery(again, self.telegram).send(
            "news", "Old", "stable-key")
        self.assertEqual("legacy-delivery", old_result["delivery_id"])
        again.delete_destination("news", route["binding_id"])
        with again.connect() as db:
            legacy_archive = db.execute(
                "SELECT * FROM archived_summaries WHERE binding_id=?",
                (route["binding_id"],)).fetchone()
        self.assertEqual("800", legacy_archive["message_id"])


if __name__ == "__main__":
    unittest.main()
