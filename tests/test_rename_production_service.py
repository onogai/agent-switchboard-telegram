"""Regression tests for protected Telegram production state preflight."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest

from scripts.rename_production_service import verify_private_paths


class ProtectedStatePreflightTests(unittest.TestCase):
    def test_uses_sudo_for_state_and_secret_checks(self):
        state = Path("/root/protected/telegram")
        secret = Path("/root/protected/secrets/telegram.env")
        with patch("scripts.rename_production_service.run",
                   return_value=SimpleNamespace(returncode=0)) as runner:
            verify_private_paths(state, secret)
        self.assertEqual(
            [call.args for call in runner.call_args_list],
            [
                ("sudo", "test", "-d", str(state)),
                ("sudo", "test", "-f", str(state / "telegram.sqlite3")),
                ("sudo", "test", "-f", str(secret)),
            ],
        )
        self.assertTrue(all(call.kwargs == {"check": False}
                            for call in runner.call_args_list))

    def test_missing_database_fails_before_cutover(self):
        state = Path("/root/protected/telegram")
        secret = Path("/root/protected/secrets/telegram.env")
        responses = [
            SimpleNamespace(returncode=0),
            SimpleNamespace(returncode=1),
        ]
        with patch("scripts.rename_production_service.run",
                   side_effect=responses) as runner:
            with self.assertRaisesRegex(RuntimeError, "telegram.sqlite3"):
                verify_private_paths(state, secret)
        self.assertEqual(runner.call_count, 2)


if __name__ == "__main__":
    unittest.main()
