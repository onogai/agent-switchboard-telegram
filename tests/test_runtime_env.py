"""Production env handoff must be explicit, private and non-destructive."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "create_runtime_env.py"
spec = importlib.util.spec_from_file_location("create_runtime_env", SCRIPT)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RuntimeEnvHandoffTests(unittest.TestCase):
    def test_only_telegram_fields_exported_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "private.env"
            fake = [{"Config": {"Env": [
                "TELEGRAM_ENCRYPTION_KEY=one=two",
                "TELEGRAM_ADMIN_TOKEN=" + "x" * 48,
                "UNRELATED_SECRET=never-export",
            ]}}]
            with patch.object(module.subprocess, "check_output",
                              return_value=json.dumps(fake)):
                env = module.get_container_env("old-telegram")
            module.save_env(path, env)
            raw = path.read_text()
            self.assertIn("TELEGRAM_ENCRYPTION_KEY=one=two", raw)
            self.assertIn("TELEGRAM_ADMIN_TOKEN=", raw)
            self.assertNotIn("UNRELATED_SECRET", raw)
            self.assertNotIn("never-export", raw)
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            with self.assertRaises(FileExistsError):
                module.save_env(path, env)
            self.assertEqual(raw, path.read_text())

    def test_missing_admin_token_rejected(self):
        fake = [{"Config": {"Env": ["TELEGRAM_ENCRYPTION_KEY=key-only"]}}]
        with patch.object(module.subprocess, "check_output",
                          return_value=json.dumps(fake)):
            with self.assertRaisesRegex(RuntimeError, "admin token"):
                module.get_container_env("old-telegram")

    def test_multiline_values_rejected(self):
        fake = [{"Config": {"Env": [
            "TELEGRAM_ENCRYPTION_KEY=key",
            "TELEGRAM_ADMIN_TOKEN=" + "x" * 48 + "\nunsafe",
        ]}}]
        with patch.object(module.subprocess, "check_output",
                          return_value=json.dumps(fake)):
            with self.assertRaisesRegex(RuntimeError, "multiline"):
                module.get_container_env("old-telegram")


if __name__ == "__main__":
    unittest.main()
