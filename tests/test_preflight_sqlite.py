"""Regression tests for a non-destructive Telegram SQLite cutover preflight."""
import importlib.util
import os
import subprocess
import sys
import sqlite3
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet
from telegram_service.store import Store

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "preflight_sqlite.py"
spec = importlib.util.spec_from_file_location("telegram_preflight_sqlite", SCRIPT)
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)


class TelegramPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "live.sqlite3"
        self.key = Fernet.generate_key().decode()
        self.store = Store(self.path, self.key)
        self.store.add_bot("123", "testbot", "Bot", "sensitive-bot-token")
        self.store.add_destination("news", "123", "-100123456", "News", "channel")

    def test_live_file_is_unchanged_and_preserves_records(self):
        before = self.path.read_bytes()
        result = preflight.audit(self.path, self.key)
        self.assertEqual(1, result["bots"])
        self.assertEqual(1, result["destinations"])
        self.assertEqual(before, self.path.read_bytes())

    def test_direct_cli_works_without_pythonpath_from_other_cwd(self):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)
        env["TELEGRAM_ENCRYPTION_KEY"] = self.key
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--db", str(self.path)],
            cwd=self.temp.name, env=env, capture_output=True, text=True,
            timeout=20, check=False)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("PASS Telegram SQLite", result.stdout)
        self.assertNotIn("sensitive-bot-token", result.stdout)

    def test_wrong_decryption_key_fails_safely(self):
        with self.assertRaisesRegex(RuntimeError, "cannot be decrypted"):
            preflight.audit(self.path, Fernet.generate_key().decode())

    def test_snapshot_validates_legacy_additive_migration(self):
        # Remove the recently added tables/columns from a disposable source
        # to exercise migration without modifying the sole source file.
        with sqlite3.connect(self.path) as db:
            db.execute("DROP TABLE archived_summaries")
            db.execute("DROP TABLE destination_bindings")
            db.execute("UPDATE destinations SET binding_id=NULL")
        result = preflight.audit(self.path, self.key)
        self.assertEqual(1, result["bots"])
        self.assertEqual(1, result["destinations"])

    def test_invalid_database_rejected(self):
        other = Path(self.temp.name) / "not-telegram.sqlite3"
        sqlite3.connect(other).close()
        with self.assertRaisesRegex(RuntimeError, "expected Telegram tables"):
            preflight.audit(other, self.key)

    def test_unreadable_source_reports_permission_cause(self):
        if os.geteuid() == 0:
            self.skipTest("root can read mode-000 files")
        self.path.chmod(0o000)
        try:
            with self.assertRaisesRegex(RuntimeError, "not readable by this user"):
                preflight.audit(self.path, self.key)
        finally:
            self.path.chmod(0o600)

    def test_missing_source_fails_closed(self):
        with self.assertRaisesRegex(RuntimeError, "existing regular SQLite"):
            preflight.audit(Path(self.temp.name) / "missing.sqlite3", self.key)


if __name__ == "__main__":
    unittest.main()
