"""Contract checks for private Telegram management API; isolated from network/production."""
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from cryptography.fernet import Fernet

import telegram_service.app as module
from telegram_service.service import TelegramDelivery
from telegram_service.bot_api import BotApiError
from telegram_service.store import Store


class FakeAPI:
    def __init__(self):
        self.messages = []
        self.pin_error = None
        self.pin_calls = []

    def call(self, token, method, payload=None):
        if method == "getMe":
            return {"id": 111, "username": "mybot"}
        if method == "getChat":
            cid = int(payload["chat_id"])
            return {"id": cid, "type": "supergroup" if cid == -10098765 else "channel"}
        if method == "sendMessage":
            self.messages.append(payload)
            return {"message_id": len(self.messages)}
        if method == "pinChatMessage":
            self.pin_calls.append(payload)
            if self.pin_error:
                raise self.pin_error
            return {"message_id": payload["message_id"]}
        if method == "getChatMember":
            return {"status": "administrator", "can_pin_messages": False,
                    "can_edit_messages": True}
        if method == "editMessageText":
            return {"message_id": payload["message_id"]}
        raise AssertionError(method)


class ManagementContract(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.api = FakeAPI()
        self.store = Store(Path(self.temp.name) / "data.sqlite3", Fernet.generate_key().decode())
        self.service = TelegramDelivery(self.store, self.api)
        self.old = module._service
        module._service = self.service
        self.addCleanup(setattr, module, "_service", self.old)
        self.secret = "x" * 48
        env = patch.dict(os.environ, TELEGRAM_ADMIN_TOKEN=self.secret)
        env.start()
        self.addCleanup(env.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=module.app),
                                        base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)

    def auth(self):
        return {"Authorization": "Bearer " + self.secret}

    async def test_authentication_guards_all_admin_operations(self):
        for path in ("status", "bots", "destinations", "deliveries"):
            response = await self.client.get("/internal/" + path)
            self.assertEqual(401, response.status_code, path)
        response = await self.client.post("/internal/bots",
                                          json={"label": "bad", "token": "123"})
        self.assertEqual(401, response.status_code)
        response = await self.client.get("/internal/status", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(401, response.status_code)

    async def test_admin_can_set_up_destinations_and_test_send(self):
        bot = await self.client.post("/internal/bots", headers=self.auth(),
                                     json={"token": "secret_token", "label": "Main"})
        self.assertEqual(200, bot.status_code, bot.text)
        self.assertEqual("111", bot.json()["id"])
        dest = await self.client.post("/internal/destinations", headers=self.auth(),
                                      json={"slug": "news", "label": "News", "bot_id": "111",
                                            "chat_id": "-100123"})
        self.assertEqual(200, dest.status_code, dest.text)
        self.assertEqual("news", dest.json()["slug"])
        listed = await self.client.get("/internal/destinations", headers=self.auth())
        self.assertEqual(1, len(listed.json()["destinations"]))
        self.assertNotIn("secret_token", listed.text)
        no_confirmation = await self.client.post("/internal/test-send", headers=self.auth(),
                                                  json={"destination": "news", "text": "Hello"})
        self.assertEqual(400, no_confirmation.status_code)
        result = await self.client.post("/internal/test-send", headers=self.auth(),
                                        json={"destination": "news", "text": "Hello", "confirm": True})
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual("accepted", result.json()["status"])
        self.assertEqual(1, len(self.api.messages))
        disabled = await self.client.put("/internal/destinations/news", headers=self.auth(),
                                         json={"enabled": False})
        self.assertEqual(200, disabled.status_code)
        blocked = await self.client.post("/internal/test-send", headers=self.auth(),
                                         json={"destination": "news", "text": "No", "confirm": True})
        self.assertEqual(400, blocked.status_code)
        self.assertEqual(1, len(self.api.messages))

    async def test_no_arbitrary_chat_or_mutation_tool(self):
        self.service.register_bot("secret_token", "Main")
        self.service.register_destination("news", "111", "-100123", "News")
        wrong = await self.client.post("/internal/test-send", headers=self.auth(),
                                       json={"destination": "-100999", "text": "No", "confirm": True})
        self.assertEqual(400, wrong.status_code)
        method = await self.client.delete("/internal/destinations/news", headers=self.auth())
        self.assertEqual(405, method.status_code)

    async def test_discovery_requires_explicit_admin_approval(self):
        self.service.register_bot("secret_token", "Main")
        self.store.record_discovery_update("111", 10, {
            "chat_id": "-100123", "title": "New Channel",
            "chat_type": "channel", "source": "membership", "present": True,
        })
        unauthorized = await self.client.get("/internal/discovered")
        self.assertEqual(401, unauthorized.status_code)
        found = await self.client.get("/internal/discovered", headers=self.auth())
        self.assertEqual(200, found.status_code)
        self.assertEqual("New Channel", found.json()["chats"][0]["title"])
        self.assertFalse(self.store.destinations(True))
        ignored = await self.client.put("/internal/discovered/111/-100123",
            headers=self.auth(), json={"ignored": True})
        self.assertEqual(200, ignored.status_code)
        blocked = await self.client.post("/internal/discovered/111/-100123/approve",
            headers=self.auth(), json={"slug": "news", "label": "News"})
        self.assertEqual(400, blocked.status_code)
        await self.client.put("/internal/discovered/111/-100123",
            headers=self.auth(), json={"ignored": False})
        approval = await self.client.post("/internal/discovered/111/-100123/approve",
            headers=self.auth(), json={"slug": "news", "label": "News"})
        self.assertEqual(200, approval.status_code, approval.text)
        self.assertEqual("news", approval.json()["slug"])
        self.assertEqual(1, len(self.store.destinations(True)))

    async def test_summary_maintenance_admin_only_and_pin_reuses_message(self):
        self.service.register_bot("secret_token", "Main")
        self.service.register_destination("news", "111", "-100123", "News")
        created = self.service.summary("news", "news:2026-11-15", "Summary", pin=False)
        self.assertEqual("accepted", created["status"])
        target = {"destination": "news", "key": "news:2026-11-15"}
        for route in ("status", "permissions"):
            unauthorized = await self.client.post("/internal/summary/" + route, json=target)
            self.assertEqual(401, unauthorized.status_code)
        status = await self.client.post("/internal/summary/status",
                                        headers=self.auth(), json=target)
        self.assertEqual(200, status.status_code)
        self.assertEqual(created["message_id"], status.json()["message_id"])
        permissions = await self.client.post("/internal/summary/permissions",
                                             headers=self.auth(), json=target)
        self.assertEqual(200, permissions.status_code)
        self.assertEqual("can_edit_messages", permissions.json()["required_right"])
        missing_confirm = await self.client.post("/internal/summary/pin",
                                                headers=self.auth(), json=target)
        self.assertEqual(400, missing_confirm.status_code)
        self.assertEqual(0, len(self.api.pin_calls))
        self.api.pin_error = BotApiError("telegram_http_400", error_code=400,
                                         description="Bad Request: not enough rights")
        failed = await self.client.post("/internal/summary/pin", headers=self.auth(),
                                       json={**target, "confirm": True})
        self.assertEqual(200, failed.status_code)
        self.assertEqual("partial", failed.json()["status"])
        self.assertTrue(failed.json()["content_saved"])
        self.assertEqual(400, failed.json()["error_code"])
        self.assertIn("not enough rights", failed.json()["description"])
        self.assertEqual(1, len(self.api.messages))
        self.api.pin_error = None
        accepted = await self.client.post("/internal/summary/pin", headers=self.auth(),
                                         json={**target, "confirm": True})
        self.assertEqual("accepted", accepted.json()["status"])
        self.assertEqual(created["message_id"], accepted.json()["message_id"])
        self.assertEqual(1, len(self.api.messages))
        self.assertEqual(2, len(self.api.pin_calls))
        self.assertTrue(self.api.pin_calls[-1]["disable_notification"])
        again = await self.client.post("/internal/summary/pin", headers=self.auth(),
                                      json={**target, "confirm": True})
        self.assertEqual("accepted", again.json()["status"])
        self.assertEqual(2, len(self.api.pin_calls))

    async def test_destination_rebind_and_delete_auth_guards(self):
        self.service.register_bot("secret_token", "Main")
        self.service.register_destination("news", "111", "-100123", "News")
        binding = self.store.get_destination("news")["binding_id"]
        self.store.record_discovery_update("111", 10, {
            "chat_id": "-10098765", "title": "News SG",
            "chat_type": "supergroup", "source": "membership", "present": True})
        base = "/internal/destinations/news/"
        self.assertEqual(401, (await self.client.get(base + "impact")).status_code)
        impact = await self.client.get(base + "impact", headers=self.auth())
        self.assertEqual(200, impact.status_code)
        self.assertEqual(binding, impact.json()["binding_id"])
        bad_rebind = await self.client.post(base + "rebind", headers=self.auth(),
                                           json={"bot_id": "111", "chat_id": "-10098765",
                                                 "expected_binding_id": binding, "label": "News"})
        self.assertEqual(400, bad_rebind.status_code)
        good_rebind = await self.client.post(base + "rebind", headers=self.auth(), json={
            "bot_id": "111", "chat_id": "-10098765", "expected_binding_id": binding,
            "label": "News", "confirm": True})
        self.assertEqual(200, good_rebind.status_code, good_rebind.text)
        self.assertEqual("-10098765", good_rebind.json()["chat_id"])
        self.assertNotEqual(binding, good_rebind.json()["binding_id"])
        stale_delete = await self.client.post(base + "delete", headers=self.auth(),
                                               json={"expected_binding_id": binding, "confirm": True})
        self.assertEqual(400, stale_delete.status_code)
        fresh = good_rebind.json()["binding_id"]
        self.assertEqual(401, (await self.client.post(base + "delete", json={
            "expected_binding_id": fresh, "confirm": True})).status_code)
        deleted = await self.client.post(base + "delete", headers=self.auth(), json={
            "expected_binding_id": fresh, "confirm": True})
        self.assertEqual(200, deleted.status_code)
        self.assertTrue(deleted.json()["deleted"])
        self.assertEqual(0, len(self.store.destinations()))



if __name__ == "__main__":
    unittest.main()
