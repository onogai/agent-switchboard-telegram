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
