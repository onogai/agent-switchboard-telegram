"""Private SQLite ownership for Telegram bots, destinations and delivery records."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

SLUG = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


class DeliveryError(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def payload_hash(operation: str, destination: str, payload: dict) -> str:
    canonical = json.dumps([operation, destination, payload], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Store:
    def __init__(self, path: Path, encryption_key: str):
        self.path = Path(path)
        if not encryption_key:
            raise RuntimeError("TELEGRAM_ENCRYPTION_KEY is required")
        self.cipher = Fernet(encryption_key.encode("ascii"))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise RuntimeError("Telegram DB cannot be a symlink")
        self.initialize()

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=15000")
        return db

    def initialize(self):
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS bots(
                    id TEXT PRIMARY KEY, username TEXT NOT NULL, label TEXT NOT NULL,
                    encrypted_token TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS destinations(
                    slug TEXT PRIMARY KEY, bot_id TEXT NOT NULL REFERENCES bots(id),
                    chat_id TEXT NOT NULL, label TEXT NOT NULL, chat_type TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL,
                    UNIQUE(bot_id,chat_id)
                );
                CREATE TABLE IF NOT EXISTS deliveries(
                    id TEXT PRIMARY KEY, destination TEXT NOT NULL,
                    operation TEXT NOT NULL, idempotency_key TEXT UNIQUE,
                    request_hash TEXT NOT NULL, status TEXT NOT NULL,
                    message_ids TEXT NOT NULL DEFAULT '[]',
                    parts_sent INTEGER NOT NULL DEFAULT 0,
                    error_code TEXT, telegram_error_code INTEGER, error_description TEXT,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS deliveries_created ON deliveries(created_at DESC);
                CREATE TABLE IF NOT EXISTS discovery_offsets(
                    bot_id TEXT PRIMARY KEY REFERENCES bots(id),
                    next_offset INTEGER NOT NULL, last_update_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS discovered_chats(
                    bot_id TEXT NOT NULL REFERENCES bots(id),
                    chat_id TEXT NOT NULL, title TEXT NOT NULL,
                    chat_type TEXT NOT NULL, source TEXT NOT NULL,
                    present INTEGER NOT NULL DEFAULT 1,
                    ignored INTEGER NOT NULL DEFAULT 0,
                    first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                    PRIMARY KEY(bot_id, chat_id)
                );
                CREATE TABLE IF NOT EXISTS destination_bindings(
                    id TEXT PRIMARY KEY, slug TEXT NOT NULL, bot_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL, chat_type TEXT NOT NULL,
                    created_at TEXT NOT NULL, retired_at TEXT
                );
                CREATE TABLE IF NOT EXISTS archived_summaries(
                    binding_id TEXT NOT NULL REFERENCES destination_bindings(id),
                    destination TEXT NOT NULL, bot_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL, summary_key TEXT NOT NULL,
                    message_id TEXT NOT NULL, content_hash TEXT NOT NULL,
                    pinned INTEGER NOT NULL, updated_at TEXT NOT NULL,
                    archived_at TEXT NOT NULL,
                    PRIMARY KEY(binding_id, summary_key)
                );
                CREATE TABLE IF NOT EXISTS summaries(
                    destination TEXT NOT NULL REFERENCES destinations(slug),
                    summary_key TEXT NOT NULL, message_id TEXT NOT NULL,
                    content_hash TEXT NOT NULL, pinned INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL, PRIMARY KEY(destination,summary_key)
                );
            """)
        # Additive migration for deployed SQLite databases.
        with self.connect() as db:
            columns = {r["name"] for r in db.execute("PRAGMA table_info(deliveries)")}
            if "telegram_error_code" not in columns:
                db.execute("ALTER TABLE deliveries ADD COLUMN telegram_error_code INTEGER")
            if "error_description" not in columns:
                db.execute("ALTER TABLE deliveries ADD COLUMN error_description TEXT")
        # Add immutable physical binding generations without dropping existing tables.
        # This is deliberately an additive migration: old delivery IDs and summaries
        # remain in place, and no stored Bot API credentials are touched.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            dcols = {r["name"] for r in db.execute("PRAGMA table_info(destinations)")}
            if "binding_id" not in dcols:
                db.execute("ALTER TABLE destinations ADD COLUMN binding_id TEXT")
            lcols = {r["name"] for r in db.execute("PRAGMA table_info(deliveries)")}
            for column in ("binding_id", "bot_id", "chat_id", "logical_idempotency_key"):
                if column not in lcols:
                    db.execute(f"ALTER TABLE deliveries ADD COLUMN {column} TEXT")
            for row in db.execute("SELECT * FROM destinations").fetchall():
                binding = row["binding_id"] or str(uuid.uuid4())
                db.execute("""INSERT OR IGNORE INTO destination_bindings
                    (id,slug,bot_id,chat_id,chat_type,created_at)
                    VALUES(?,?,?,?,?,?)""", (binding, row["slug"], row["bot_id"],
                      row["chat_id"], row["chat_type"], row["created_at"]))
                db.execute("UPDATE destinations SET binding_id=? WHERE slug=?",
                           (binding, row["slug"]))
            # Historical delivery rows predate physical-route snapshots. Assign
            # the currently known binding (the old system never supported rebind).
            for row in db.execute(
                "SELECT id,destination,idempotency_key FROM deliveries "
                "WHERE binding_id IS NULL").fetchall():
                route = db.execute(
                    "SELECT binding_id,bot_id,chat_id FROM destinations WHERE slug=?",
                    (row["destination"],)).fetchone()
                if route:
                    binding, bot_id, chat_id = tuple(route)
                else:
                    # Orphaned/legacy data cannot be mapped to a Telegram chat.
                    # Preserve it in a separate non-routable history generation.
                    binding = str(uuid.uuid5(uuid.NAMESPACE_URL,
                                             "telegram-legacy:" + row["destination"]))
                    bot_id, chat_id = "", ""
                    db.execute("""INSERT OR IGNORE INTO destination_bindings
                        (id,slug,bot_id,chat_id,chat_type,created_at,retired_at)
                        VALUES(?,?,?,?,?,?,?)""",
                        (binding, row["destination"], "", "", "unknown", now(), now()))
                # Unique legacy keys are namespaced per binding once. New work can
                # reuse the same logical idempotency key after rebind or delete.
                scoped = f"{binding}:{row['idempotency_key']}" if row["idempotency_key"] else None
                db.execute("""UPDATE deliveries SET binding_id=?,bot_id=?,chat_id=?,
                    idempotency_key=?,logical_idempotency_key=? WHERE id=?""",
                    (binding, bot_id, chat_id, scoped, row["idempotency_key"], row["id"]))
        self.path.chmod(0o600)

    def add_bot(self, bot_id: str, username: str, label: str, token: str) -> dict:
        if not bot_id.isdigit() or not token or not label.strip():
            raise DeliveryError("Invalid bot ID, token or label")
        encrypted = self.cipher.encrypt(token.encode()).decode()
        with self.connect() as db:
            db.execute("INSERT INTO bots(id,username,label,encrypted_token,created_at) "
                       "VALUES(?,?,?,?,?)", (bot_id, username, label.strip(), encrypted, now()))
        return {"id": bot_id, "username": username, "label": label.strip(), "enabled": True}

    def list_bots(self) -> list[dict]:
        with self.connect() as db:
            return [{"id": row["id"], "username": row["username"],
                     "label": row["label"], "enabled": bool(row["enabled"])}
                    for row in db.execute("SELECT * FROM bots ORDER BY label")]

    def set_bot(self, bot_id: str, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise DeliveryError("enabled must be a boolean")
        with self.connect() as db:
            changed = db.execute("UPDATE bots SET enabled=? WHERE id=?", (int(enabled), bot_id))
            if not changed.rowcount:
                raise DeliveryError("Unknown bot")
        return {"id": bot_id, "enabled": enabled}

    def bot_token(self, bot_id: str) -> str:
        with self.connect() as db:
            row = db.execute("SELECT encrypted_token,enabled FROM bots WHERE id=?", (bot_id,)).fetchone()
        if row is None or not row["enabled"]:
            raise DeliveryError("Bot is unavailable")
        try:
            return self.cipher.decrypt(row["encrypted_token"].encode()).decode()
        except InvalidToken:
            raise RuntimeError("Telegram token cannot be decrypted") from None

    def add_destination(self, slug: str, bot_id: str, chat_id: str,
                        label: str, chat_type: str) -> dict:
        if not isinstance(slug, str) or not SLUG.fullmatch(slug):
            raise DeliveryError("Destination slug must start with a letter and contain 2-64 lowercase characters")
        if not isinstance(chat_id, str) or not re.fullmatch(r"-?[0-9]{1,20}", chat_id):
            raise DeliveryError("A numeric Telegram chat ID is required")
        if not isinstance(label, str) or not label.strip() or len(label) > 120:
            raise DeliveryError("Invalid destination label")
        if chat_type not in {"private", "group", "supergroup", "channel"}:
            raise DeliveryError("Invalid Telegram chat type")
        binding = str(uuid.uuid4())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT enabled FROM bots WHERE id=?", (bot_id,)).fetchone()
            if row is None or not row["enabled"]:
                raise DeliveryError("Bot is unavailable")
            db.execute("""INSERT INTO destinations
                (slug,bot_id,chat_id,label,chat_type,created_at,binding_id)
                VALUES(?,?,?,?,?,?,?)""",
                (slug, bot_id, chat_id, label.strip(), chat_type, now(), binding))
            db.execute("""INSERT INTO destination_bindings
                (id,slug,bot_id,chat_id,chat_type,created_at)
                VALUES(?,?,?,?,?,?)""",
                (binding, slug, bot_id, chat_id, chat_type, now()))
        return self.get_destination(slug)

    def get_destination(self, slug: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT d.*, b.enabled AS bot_enabled FROM destinations d "
                             "JOIN bots b ON b.id=d.bot_id WHERE d.slug=?", (slug,)).fetchone()
        if row is None:
            raise DeliveryError("Unknown destination")
        return {"slug": row["slug"], "bot_id": row["bot_id"], "chat_id": row["chat_id"],
                "label": row["label"], "chat_type": row["chat_type"],
                "binding_id": row["binding_id"], "enabled": bool(row["enabled"]), "bot_enabled": bool(row["bot_enabled"])}

    def destinations(self, active_only: bool = False) -> list[dict]:
        with self.connect() as db:
            slugs = [r["slug"] for r in db.execute(
                "SELECT d.slug FROM destinations d JOIN bots b ON b.id=d.bot_id "
                + ("WHERE d.enabled=1 AND b.enabled=1 " if active_only else "")
                + "ORDER BY d.slug")]
        return [self.get_destination(slug) for slug in slugs]

    def destination_impact(self, slug: str) -> dict:
        dest = self.get_destination(slug)
        with self.connect() as db:
            summaries = db.execute("SELECT COUNT(*) FROM summaries WHERE destination=?",
                                   (slug,)).fetchone()[0]
            deliveries = db.execute(
                "SELECT COUNT(*) FROM deliveries WHERE binding_id=?",
                (dest["binding_id"],)).fetchone()[0]
        return {"slug": slug, "binding_id": dest["binding_id"],
                "bot_id": dest["bot_id"], "chat_id": dest["chat_id"],
                "summary_count": summaries, "delivery_count": deliveries}

    @staticmethod
    def _archive_summaries(db, dest: sqlite3.Row) -> int:
        rows = db.execute("SELECT * FROM summaries WHERE destination=?",
                          (dest["slug"],)).fetchall()
        for entry in rows:
            db.execute("""INSERT INTO archived_summaries
                (binding_id,destination,bot_id,chat_id,summary_key,message_id,
                 content_hash,pinned,updated_at,archived_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (dest["binding_id"], dest["slug"], dest["bot_id"], dest["chat_id"],
                 entry["summary_key"], entry["message_id"], entry["content_hash"],
                 entry["pinned"], entry["updated_at"], now()))
        db.execute("DELETE FROM summaries WHERE destination=?", (dest["slug"],))
        return len(rows)

    @staticmethod
    def _expect_binding(db, slug: str, expected_binding_id: str):
        row = db.execute("SELECT * FROM destinations WHERE slug=?", (slug,)).fetchone()
        if not row:
            raise DeliveryError("Unknown destination")
        if not isinstance(expected_binding_id, str) or row["binding_id"] != expected_binding_id:
            raise DeliveryError("Destination changed since review; refresh and try again")
        return row

    def delete_destination(self, slug: str, expected_binding_id: str) -> dict:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            dest = self._expect_binding(db, slug, expected_binding_id)
            count = self._archive_summaries(db, dest)
            db.execute("UPDATE destination_bindings SET retired_at=? WHERE id=?",
                       (now(), dest["binding_id"]))
            db.execute("DELETE FROM destinations WHERE slug=?", (slug,))
        return {"deleted": True, "slug": slug, "retired_binding_id": expected_binding_id,
                "archived_summaries": count}

    def rebind_destination(self, slug: str, expected_binding_id: str,
                           bot_id: str, chat_id: str, chat_type: str,
                           label: str) -> dict:
        if not isinstance(label, str) or not label.strip() or len(label) > 120:
            raise DeliveryError("Invalid destination label")
        if chat_type not in {"private", "group", "supergroup", "channel"}:
            raise DeliveryError("Invalid Telegram chat type")
        binding = str(uuid.uuid4())
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = self._expect_binding(db, slug, expected_binding_id)
            candidate = db.execute("""SELECT present,ignored FROM discovered_chats
                WHERE bot_id=? AND chat_id=?""", (bot_id, chat_id)).fetchone()
            if not candidate or not candidate["present"] or candidate["ignored"]:
                raise DeliveryError("Target must be a present, non-ignored discovered chat")
            active_bot = db.execute("SELECT enabled FROM bots WHERE id=?", (bot_id,)).fetchone()
            if not active_bot or not active_bot["enabled"]:
                raise DeliveryError("Bot is unavailable")
            if old["bot_id"] == bot_id and old["chat_id"] == chat_id:
                raise DeliveryError("Destination already points to this chat")
            used = db.execute("""SELECT slug FROM destinations
                WHERE bot_id=? AND chat_id=? AND slug<>?""",
                (bot_id, chat_id, slug)).fetchone()
            if used:
                raise DeliveryError("Chat already belongs to destination " + used["slug"])
            self._archive_summaries(db, old)
            db.execute("UPDATE destination_bindings SET retired_at=? WHERE id=?",
                       (now(), old["binding_id"]))
            db.execute("""INSERT INTO destination_bindings
                (id,slug,bot_id,chat_id,chat_type,created_at)
                VALUES(?,?,?,?,?,?)""",
                (binding, slug, bot_id, chat_id, chat_type, now()))
            db.execute("""UPDATE destinations
                SET bot_id=?,chat_id=?,chat_type=?,label=?,binding_id=?
                WHERE slug=?""",
                (bot_id, chat_id, chat_type, label.strip(), binding, slug))
        return self.get_destination(slug)

    def set_destination(self, slug: str, enabled: bool) -> dict:
        if type(enabled) is not bool:
            raise DeliveryError("enabled must be a boolean")
        with self.connect() as db:
            changed = db.execute("UPDATE destinations SET enabled=? WHERE slug=?", (int(enabled), slug))
            if not changed.rowcount:
                raise DeliveryError("Unknown destination")
        return self.get_destination(slug)

    def active_target(self, slug: str) -> tuple[dict, str]:
        dest = self.get_destination(slug)
        if not dest["enabled"] or not dest["bot_enabled"]:
            raise DeliveryError("Destination is disabled")
        return dest, self.bot_token(dest["bot_id"])

    def begin(self, destination: str, operation: str, payload: dict,
              key: str | None, binding_id: str | None = None) -> tuple[dict, bool]:
        if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 180):
            raise DeliveryError("Invalid idempotency key")
        fingerprint = payload_hash(operation, destination, payload)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            route = db.execute("SELECT * FROM destinations WHERE slug=?",
                               (destination,)).fetchone()
            if route is None:
                raise DeliveryError("Unknown destination")
            if binding_id is not None and route["binding_id"] != binding_id:
                raise DeliveryError("Destination binding changed; retry with fresh route")
            binding_id = route["binding_id"]
            scoped_key = f"{binding_id}:{key}" if key else None
            if key:
                old = db.execute("SELECT * FROM deliveries WHERE idempotency_key=?",
                                 (scoped_key,)).fetchone()
                if old:
                    if old["request_hash"] != fingerprint:
                        raise DeliveryError("Idempotency key reused with different request")
                    value = self._delivery(old)
                    # An abandoned in_progress record can mean that Telegram
                    # accepted a request before our process crashed.
                    if value["status"] == "in_progress":
                        db.execute("UPDATE deliveries SET status='unknown',"
                                   "error_code='interrupted_attempt',updated_at=? WHERE id=?",
                                   (now(), value["delivery_id"]))
                        value["status"] = "unknown"
                        value["error_code"] = "interrupted_attempt"
                    return value, False
            delivery_id = str(uuid.uuid4())
            db.execute("""INSERT INTO deliveries
                (id,destination,operation,idempotency_key,request_hash,
                 status,created_at,updated_at,binding_id,bot_id,chat_id,logical_idempotency_key)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (delivery_id, destination, operation, scoped_key, fingerprint,
                 "in_progress", now(), now(), binding_id,
                 route["bot_id"], route["chat_id"], key))
            row = db.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            return self._delivery(row), True

    @staticmethod
    def _delivery(row) -> dict:
        return {"delivery_id": row["id"], "destination": row["destination"],
                "binding_id": row["binding_id"], "bot_id": row["bot_id"],
                "chat_id": row["chat_id"], "operation": row["operation"], "status": row["status"],
                "message_ids": json.loads(row["message_ids"]),
                "logical_idempotency_key": row["logical_idempotency_key"],
                "parts_sent": row["parts_sent"],
                "error_code": row["telegram_error_code"] if row["telegram_error_code"] is not None
                              else row["error_code"],
                "error_type": row["error_code"], "description": row["error_description"]}

    def finish(self, delivery_id: str, status: str, ids: list[str],
               error_code: str | None = None, telegram_error_code: int | None = None,
               description: str | None = None) -> dict:
        if status not in {"accepted", "failed", "unknown", "partial"}:
            raise DeliveryError("Invalid delivery status")
        with self.connect() as db:
            db.execute("UPDATE deliveries SET status=?,message_ids=?,parts_sent=?,error_code=?,telegram_error_code=?,error_description=?,updated_at=? "
                       "WHERE id=?", (status, json.dumps(ids), len(ids), error_code, telegram_error_code, description, now(), delivery_id))
            row = db.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        return self._delivery(row)

    def recent(self, limit=50) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise DeliveryError("Invalid limit")
        with self.connect() as db:
            return [self._delivery(r) for r in db.execute(
                "SELECT * FROM deliveries ORDER BY created_at DESC LIMIT ?", (limit,))]

    def summary(self, destination: str, key: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM summaries WHERE destination=? AND summary_key=?",
                             (destination, key)).fetchone()
        return dict(row) if row else None

    def save_summary(self, destination: str, key: str, message_id: str,
                     content_hash: str, pinned: bool):
        with self.connect() as db:
            db.execute("INSERT INTO summaries(destination,summary_key,message_id,content_hash,pinned,updated_at) "
                       "VALUES(?,?,?,?,?,?) ON CONFLICT(destination,summary_key) DO UPDATE SET "
                       "message_id=excluded.message_id,content_hash=excluded.content_hash,"
                       "pinned=excluded.pinned,updated_at=excluded.updated_at",
                       (destination, key, message_id, content_hash, int(pinned), now()))

    # Discovery is private Admin state, never a public MCP allowlist.
    def discovery_offset(self, bot_id: str) -> int | None:
        from datetime import timedelta
        with self.connect() as db:
            row = db.execute(
                "SELECT next_offset,last_update_at FROM discovery_offsets WHERE bot_id=?",
                (bot_id,)).fetchone()
            if row is None:
                return None
            # Telegram may choose a fresh random update_id after a week of inactivity.
            if datetime.fromisoformat(row["last_update_at"]) < datetime.now(timezone.utc) - timedelta(days=7):
                db.execute("DELETE FROM discovery_offsets WHERE bot_id=?", (bot_id,))
                return None
            return row["next_offset"]

    def record_discovery_update(self, bot_id: str, update_id: int, chat: dict | None):
        with self.connect() as db:
            cursor = db.execute(
                "SELECT next_offset FROM discovery_offsets WHERE bot_id=?", (bot_id,)).fetchone()
            if cursor and update_id < cursor["next_offset"]:
                return
            stamp = now()
            if chat:
                db.execute("""
                    INSERT INTO discovered_chats
                    (bot_id,chat_id,title,chat_type,source,present,first_seen,last_seen)
                    VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(bot_id,chat_id) DO UPDATE SET
                        title=excluded.title, chat_type=excluded.chat_type,
                        source=excluded.source, present=excluded.present,
                        last_seen=excluded.last_seen
                    """, (bot_id, chat["chat_id"], chat["title"], chat["chat_type"],
                          chat["source"], int(chat["present"]), stamp, stamp))
            db.execute("""
                INSERT INTO discovery_offsets(bot_id,next_offset,last_update_at)
                VALUES(?,?,?) ON CONFLICT(bot_id) DO UPDATE SET
                    next_offset=excluded.next_offset, last_update_at=excluded.last_update_at
                """, (bot_id, update_id + 1, stamp))

    def discovered_chats(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("""
                SELECT c.*, b.username AS bot_username, b.label AS bot_label,
                       b.enabled AS bot_enabled
                FROM discovered_chats c JOIN bots b ON b.id=c.bot_id
                LEFT JOIN destinations d ON d.bot_id=c.bot_id AND d.chat_id=c.chat_id
                WHERE d.slug IS NULL
                ORDER BY c.ignored ASC, c.last_seen DESC LIMIT 200
            """).fetchall()
            return [{
                "bot_id": row["bot_id"], "bot_username": row["bot_username"],
                "bot_label": row["bot_label"], "bot_enabled": bool(row["bot_enabled"]),
                "chat_id": row["chat_id"], "title": row["title"],
                "chat_type": row["chat_type"], "source": row["source"],
                "present": bool(row["present"]), "ignored": bool(row["ignored"]),
                "first_seen": row["first_seen"], "last_seen": row["last_seen"],
            } for row in rows]

    def discovered_chat(self, bot_id: str, chat_id: str) -> dict:
        for row in self.discovered_chats():
            if row["bot_id"] == bot_id and row["chat_id"] == chat_id:
                return row
        raise DeliveryError("Discovered chat is not available")

    def set_discovered_ignored(self, bot_id: str, chat_id: str, ignored: bool) -> dict:
        if type(ignored) is not bool:
            raise DeliveryError("ignored must be boolean")
        with self.connect() as db:
            result = db.execute(
                "UPDATE discovered_chats SET ignored=? WHERE bot_id=? AND chat_id=?",
                (int(ignored), bot_id, chat_id))
            if not result.rowcount:
                raise DeliveryError("Unknown discovered chat")
        return {"bot_id": bot_id, "chat_id": chat_id, "ignored": ignored}

    def set_summary_pinned(self, destination: str, key: str) -> dict:
        """Mark only an existing summary pinned; no new records/messages."""
        with self.connect() as db:
            result = db.execute(
                "UPDATE summaries SET pinned=1,updated_at=? WHERE destination=? AND summary_key=?",
                (now(), destination, key))
            if not result.rowcount:
                raise DeliveryError("Unknown summary")
        return self.summary(destination, key)
