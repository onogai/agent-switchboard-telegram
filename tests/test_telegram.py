"""Standalone Telegram service import and published MCP contract checks."""
import asyncio
import importlib.abc
import sys
import unittest


class RejectCore(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "gateway" or fullname.startswith("gateway."):
            raise ImportError("Telegram add-on must not import Gateway Core")


class TelegramBoundaryTests(unittest.TestCase):
    def test_legacy_send_compatibility_is_single_message_and_never_retries_implicitly(self):
        import os
        from unittest.mock import patch
        import telegram_service.app as module

        received = []

        async def fake_send(destination, text, idempotency_key=None):
            received.append((destination, text, idempotency_key))
            return {"status": "accepted", "message_ids": ["12345"]}

        with patch.dict(os.environ, {"TELEGRAM_DEFAULT_DESTINATION": "alerts"}):
            with patch.object(module, "telegram_send", new=fake_send):
                self.assertEqual(asyncio.run(module.send_telegram("Test message")),
                                 "Telegram message sent (ID: 12345).")
                with self.assertRaises(ValueError):
                    asyncio.run(module.send_telegram("😀" * 3000))
        self.assertEqual(received, [("alerts", "Test message", None)])

    def test_no_gateway_import_and_mcp_catalog(self):
        blocker = RejectCore()
        sys.meta_path.insert(0, blocker)
        try:
            from telegram_service.app import mcp
            catalog = asyncio.run(mcp.list_tools())
            self.assertEqual({t.name for t in catalog},
                             {"list_destinations", "telegram_send", "telegram_upsert_summary", "send_telegram"})
            self.assertFalse(any(s == "gateway" or s.startswith("gateway.")
                                 for s in sys.modules))
        finally:
            sys.meta_path.remove(blocker)


if __name__ == "__main__":
    unittest.main()
