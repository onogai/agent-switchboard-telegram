"""Offline schema-v2 Telegram adoption and credential compatibility tests."""
from __future__ import annotations

import os
from pathlib import Path
import sqlite3
from contextlib import closing
import tempfile
import unittest
from unittest.mock import patch

from cryptography.fernet import Fernet

import telegram_service.app as module
from telegram_service.store import Store
from scripts.adopt_existing import migrate, _write_secret


class SecretCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_managed_generated_seed_is_stable_and_fernet_compatible(self):
        path = self.folder / "seed"
        path.write_text("a" * 64)
        with patch.dict(os.environ, {"TELEGRAM_ENCRYPTION_KEY_FILE": str(path)}):
            key = module._encryption_key()
            self.assertEqual(key, module._encryption_key())
            self.assertEqual(44, len(key))
            Fernet(key)

    def test_managed_imported_fernet_key_is_preserved(self):
        legacy = Fernet.generate_key().decode()
        path = self.folder / "secret"
        path.write_text(legacy + "\n")
        with patch.dict(os.environ, {"TELEGRAM_ENCRYPTION_KEY_FILE": str(path)}):
            self.assertEqual(legacy, module._encryption_key())

    def test_file_secrets_take_precedence_over_legacy_environment(self):
        path = self.folder / "admin"
        path.write_text("m" * 48 + "\n")
        with patch.dict(os.environ, {"TELEGRAM_ADMIN_TOKEN": "incorrect",
                                     "TELEGRAM_ADMIN_TOKEN_FILE": str(path)}):
            self.assertEqual("m" * 48, module._private_secret("TELEGRAM_ADMIN_TOKEN"))

    def test_discovery_gate_prevents_second_receiver(self):
        gate = self.folder / 'discovery.enabled'
        with patch.dict(os.environ, {'TELEGRAM_DISCOVERY_GATE_FILE': str(gate)}):
            self.assertFalse(module._discovery_gate_open())
            gate.touch()
            self.assertTrue(module._discovery_gate_open())
            gate.unlink()
            self.assertFalse(module._discovery_gate_open())

    def test_discovery_is_explicitly_disabled_for_staging(self):
        with patch.dict(os.environ, {"TELEGRAM_DISCOVERY_ENABLED": "0"}):
            self.assertFalse(module._discovery_enabled())
        with patch.dict(os.environ, {"TELEGRAM_DISCOVERY_ENABLED": "1"}):
            self.assertTrue(module._discovery_enabled())
        with patch.dict(os.environ, {"TELEGRAM_DISCOVERY_ENABLED": "garbage"}):
            with self.assertRaises(RuntimeError):
                module._discovery_enabled()


class OfflineAdoptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.instance = self.root / "addons" / "telegramhub"
        self.state = self.instance / "state"
        (self.state / "database").mkdir(parents=True)
        (self.state / "secrets").mkdir()
        (self.instance / "source").mkdir()
        (self.instance / "source" / "switchboard-addon.yaml").write_text("schema: 2\n")
        import json
        (self.instance / "installation.json").write_text(json.dumps({
            "instance": "telegramhub", "source_id": "telegramhub",
            "addon_id": "telegramhub", "schema": "2", "target": "production",
            "staged_replacement": {"old_url": "http://telegram-delivery:8781/mcp/",
                                   "revision": 0},
        }))
        (self.state / "secrets" / "encryptionkey").write_text("a" * 64 + "\n")
        (self.state / "secrets" / "admintoken").write_text("b" * 64 + "\n")
        self.old_db = self.root / "legacy.sqlite3"
        self.legacy_key = Fernet.generate_key().decode()
        old = Store(self.old_db, self.legacy_key)
        old.add_bot("101", "sample", "Main", "secret_token")
        self.env_file = self.root / "telegram.env"
        self.env_file.write_text("TELEGRAM_ENCRYPTION_KEY=" + self.legacy_key +
                                 "\nTELEGRAM_ADMIN_TOKEN=" + "z" * 48 + "\n")

    def _run(self, apply=False, old_running=False, candidate_running=False):
        with patch("scripts.adopt_existing._legacy_status_and_identity",
                   return_value=old_running), patch(
                       "scripts.adopt_existing._candidate_running",
                       return_value=candidate_running):
            return migrate(self.instance, self.old_db, self.env_file, apply)

    def test_read_only_preflight_preserves_both_databases(self):
        counts = self._run()
        self.assertEqual(1, counts["bots"])
        self.assertFalse((self.state / "database" / "telegram.sqlite3").exists())
        self.assertFalse((self.state / ".telegram-adopted").exists())

    def test_offline_adoption_preserves_credentials_and_backup(self):
        with closing(sqlite3.connect(self.old_db)) as db:
            before = db.execute("SELECT encrypted_token FROM bots WHERE id=?", ("101",)).fetchone()[0]
        result = self._run(apply=True)
        self.assertEqual(1, result["bots"])
        self.assertTrue((self.state / ".telegram-adopted").exists())
        self.assertEqual(self.legacy_key, (self.state / "secrets" / "encryptionkey").read_text().strip())
        self.assertEqual("z" * 48, (self.state / "secrets" / "admintoken").read_text().strip())
        backup, = list((self.state / "legacy-backup").glob("legacy-*.sqlite3"))
        self.assertTrue(backup.is_file())
        with closing(sqlite3.connect(self.old_db)) as db:
            after = db.execute("SELECT encrypted_token FROM bots WHERE id=?", ("101",)).fetchone()[0]
        self.assertEqual(before, after)
        managed = Store(self.state / "database" / "telegram.sqlite3", self.legacy_key)
        self.assertEqual("secret_token", managed.bot_token("101"))
        with self.assertRaisesRegex(RuntimeError, "already imported"):
            self._run(apply=True)

    def test_refuses_running_legacy_or_candidate_on_apply(self):
        with self.assertRaisesRegex(RuntimeError, "Stop the legacy"):
            self._run(apply=True, old_running=True)
        with self.assertRaisesRegex(RuntimeError, "Stop the candidate"):
            self._run(apply=True, candidate_running=True)
        self.assertEqual(1, self._run(old_running=True)["bots"])

    def test_empty_candidate_with_wal_shm_sidecars_can_be_migrated(self):
        # The staged add-on creates SQLite in WAL mode on its first startup.
        candidate_path = self.state / "database" / "telegram.sqlite3"
        Store(candidate_path, Fernet.generate_key().decode())
        # Simulate sidecars surviving the stopped candidate process.
        for suffix in ("-wal", "-shm"):
            Path(str(candidate_path) + suffix).touch()
        self.assertEqual(1, self._run()["bots"])  # read-only preflight
        self.assertEqual(1, self._run(apply=True)["bots"])
        self.assertFalse(Path(str(candidate_path) + "-wal").exists())
        self.assertFalse(Path(str(candidate_path) + "-shm").exists())
        with __import__("sqlite3").connect(candidate_path) as db:
            self.assertEqual("ok", db.execute("PRAGMA integrity_check").fetchone()[0])
        managed = Store(candidate_path, self.legacy_key)
        self.assertEqual("secret_token", managed.bot_token("101"))

    def test_replaced_secrets_keep_private_mode_and_owner(self):
        secret = self.state / "secrets" / "admintoken"
        before = secret.stat()
        _write_secret(secret, "replacement")
        after = secret.stat()
        self.assertEqual((before.st_uid, before.st_gid), (after.st_uid, after.st_gid))
        self.assertEqual(0o600, after.st_mode & 0o777)
        self.assertEqual("replacement", secret.read_text().strip())

    def test_sidecar_symlink_is_rejected(self):
        candidate_path = self.state / "database" / "telegram.sqlite3"
        Store(candidate_path, Fernet.generate_key().decode())
        wal = self.state / "database" / "telegram.sqlite3-wal"
        wal.unlink(missing_ok=True)
        wal.symlink_to(self.env_file)
        with self.assertRaisesRegex(RuntimeError, "non-symlink"):
            self._run()

    def test_refuses_to_overwrite_nonempty_candidate(self):
        candidate = Store(self.state / "database" / "telegram.sqlite3", Fernet.generate_key().decode())
        candidate.add_bot("333", "wrong", "Already live", "another")
        with self.assertRaisesRegex(RuntimeError, "not empty"):
            self._run(apply=True)


if __name__ == "__main__":
    unittest.main()
