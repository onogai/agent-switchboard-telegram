"""Telegram delivery facade. Owns all routing, bot API and persistent results."""
from __future__ import annotations

import hashlib
from threading import RLock

from .bot_api import BotApi, BotApiError
from .store import DeliveryError, Store

MAX_UNITS = 16384
PART_UNITS = 4096


def units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def split_text(text: str) -> list[str]:
    if not isinstance(text, str) or not text.strip() or units(text) > MAX_UNITS:
        raise DeliveryError("Message must be nonempty and at most 16384 UTF-16 units")
    parts = []
    current = []
    size = 0
    for char in text:
        width = units(char)
        if size + width > PART_UNITS:
            parts.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    if current:
        parts.append("".join(current))
    return parts


class TelegramDelivery:
    def __init__(self, store: Store, api: BotApi | None = None):
        self.store = store
        self.api = api or BotApi()
        # Serialize summary create/edit/pin within the single Hub process.
        self._summary_lock = RLock()

    def register_bot(self, token: str, label: str) -> dict:
        if not isinstance(token, str) or not isinstance(label, str):
            raise DeliveryError("Invalid bot credentials")
        result = self.api.call(token, "getMe")
        return self.store.add_bot(str(result["id"]), result.get("username", ""), label, token)

    def register_destination(self, slug: str, bot_id: str, chat_id: str,
                             label: str) -> dict:
        token = self.store.bot_token(bot_id)
        result = self.api.call(token, "getChat", {"chat_id": chat_id})
        actual = str(result["id"])
        if actual != chat_id:
            raise DeliveryError("Telegram returned a different chat ID")
        with self._summary_lock:
            return self.store.add_destination(slug, bot_id, actual, label, result["type"])

    def delete_destination(self, slug: str, expected_binding_id: str) -> dict:
        with self._summary_lock:
            return self.store.delete_destination(slug, expected_binding_id)

    def rebind_destination(self, slug: str, expected_binding_id: str,
                           bot_id: str, chat_id: str, label: str) -> dict:
        token = self.store.bot_token(bot_id)
        result = self.api.call(token, "getChat", {"chat_id": chat_id})
        if str(result["id"]) != chat_id:
            raise DeliveryError("Telegram returned a different chat ID")
        with self._summary_lock:
            return self.store.rebind_destination(
                slug, expected_binding_id, bot_id, chat_id, result["type"], label)

    def send(self, destination: str, text: str, idempotency_key: str | None = None) -> dict:
        with self._summary_lock:
            return self._send_locked(destination, text, idempotency_key)

    def _send_locked(self, destination: str, text: str, idempotency_key: str | None = None) -> dict:
        parts = split_text(text)
        target, token = self.store.active_target(destination)
        record, first = self.store.begin(destination, "send", {"text": text}, idempotency_key,
                                         target["binding_id"])
        if not first:
            return record
        ids = []
        try:
            for part in parts:
                response = self.api.call(token, "sendMessage",
                                         {"chat_id": target["chat_id"], "text": part})
                ids.append(str(response["message_id"]))
        except BotApiError as exc:
            status = "unknown" if exc.uncertain else "partial" if ids else "failed"
            if ids and exc.uncertain:
                status = "partial"
            return self.store.finish(record["delivery_id"], status, ids,
                                     exc.code, exc.error_code, exc.description)
        return self.store.finish(record["delivery_id"], "accepted", ids)

    @staticmethod
    def _api_error(exc: BotApiError) -> dict:
        return {"error_type": exc.code, "error_code": exc.error_code,
                "description": exc.description}

    def pin_capability(self, destination: str) -> dict:
        """Read Telegram's *current* bot membership and required pin permission."""
        target, token = self.store.active_target(destination)
        result = self.api.call(token, "getChatMember", {
            "chat_id": target["chat_id"], "user_id": int(target["bot_id"]),
        })
        if not isinstance(result, dict):
            raise BotApiError("telegram_invalid_response", uncertain=True)
        status = result.get("status")
        chat_type = target["chat_type"]
        required = ("can_edit_messages" if chat_type == "channel"
                    else "can_pin_messages" if chat_type in ("group", "supergroup")
                    else None)
        if required is None:
            allowed = None  # Private chats follow different Bot API rules.
        elif status == "creator":
            allowed = True
        else:
            allowed = status == "administrator" and result.get(required) is True
        return {
            "destination": destination, "chat_type": chat_type,
            "bot_status": status, "required_right": required, "can_pin": allowed,
        }

    def summary_record(self, destination: str, key: str) -> dict:
        self.store.active_target(destination)
        existing = self.store.summary(destination, key)
        if existing is None:
            raise DeliveryError("Unknown summary")
        return {"destination": destination, "key": key, "message_id": existing["message_id"],
                "content_saved": True, "pinned": bool(existing["pinned"]),
                "updated_at": existing["updated_at"]}

    def _pin_summary_locked(self, destination: str, key: str, check_remote: bool = False) -> dict:
        target, token = self.store.active_target(destination)
        existing = self.store.summary(destination, key)
        if existing is None:
            raise DeliveryError("Unknown summary; refusing to send or edit")
        message_id = existing["message_id"]
        if existing["pinned"]:
            return {"status": "accepted", "message_id": message_id, "content_saved": True,
                    "pin_status": "pinned", "unchanged": True}
        if check_remote:
            try:
                chat = self.api.call(token, "getChat", {"chat_id": target["chat_id"]})
                pinned = chat.get("pinned_message") if isinstance(chat, dict) else None
                if isinstance(pinned, dict) and str(pinned.get("message_id")) == message_id:
                    self.store.set_summary_pinned(destination, key)
                    return {"status": "accepted", "message_id": message_id,
                            "content_saved": True, "pin_status": "pinned",
                            "unchanged": True, "verified": True}
            except BotApiError as exc:
                return {"status": "partial", "message_id": message_id,
                        "content_saved": True, "pin_status": "unknown",
                        "retry_scope": "pin_only", "unchanged": True,
                        **self._api_error(exc)}
        try:
            self.api.call(token, "pinChatMessage", {
                "chat_id": target["chat_id"], "message_id": int(message_id),
                "disable_notification": True,
            })
        except BotApiError as exc:
            # An earlier uncertain pin may have succeeded; never pin it again.
            if exc.description and "already pinned" in exc.description.lower():
                self.store.set_summary_pinned(destination, key)
                return {"status": "accepted", "message_id": message_id,
                        "content_saved": True, "pin_status": "pinned",
                        "unchanged": True, "verified": True}
            # Pin is independent from already persisted content. Queue must not
            # rerun the digest; only this stored message can be retried.
            return {
                "status": "partial", "message_id": message_id, "content_saved": True,
                "pin_status": "unknown" if exc.uncertain else "failed",
                "retry_scope": "pin_only", "unchanged": True,
                **self._api_error(exc),
            }
        self.store.set_summary_pinned(destination, key)
        return {"status": "accepted", "message_id": message_id, "content_saved": True,
                "pin_status": "pinned", "unchanged": True}

    def pin_summary(self, destination: str, key: str) -> dict:
        """Retry pin on a stored message ID; NEVER call sendMessage/editMessageText."""
        with self._summary_lock:
            return self._pin_summary_locked(destination, key, check_remote=True)

    def summary(self, destination: str, key: str, text: str, pin: bool = True) -> dict:
        if not isinstance(key, str) or not 1 <= len(key) <= 120:
            raise DeliveryError("Invalid summary key")
        if type(pin) is not bool:
            raise DeliveryError("pin must be boolean")
        if not isinstance(text, str) or not text.strip() or units(text) > PART_UNITS:
            raise DeliveryError("Summary must fit in one Telegram message (4096 UTF-16 units)")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._summary_lock:
            target, token = self.store.active_target(destination)
            existing = self.store.summary(destination, key)
            message_id = existing["message_id"] if existing else None
            changed = not existing or existing["content_hash"] != digest

            if not changed:
                if pin and not existing["pinned"]:
                    return self._pin_summary_locked(destination, key, check_remote=True)
                return {"status": "accepted", "message_id": message_id,
                        "content_saved": True,
                        "pin_status": "pinned" if existing["pinned"] else "not_requested",
                        "unchanged": True}

            try:
                if existing is None:
                    response = self.api.call(token, "sendMessage", {
                        "chat_id": target["chat_id"], "text": text,
                    })
                    message_id = str(response["message_id"])
                else:
                    self.api.call(token, "editMessageText", {
                        "chat_id": target["chat_id"], "message_id": int(message_id),
                        "text": text,
                    })
            except BotApiError as exc:
                return {
                    "status": "unknown" if exc.uncertain else "failed",
                    "message_id": message_id, "content_saved": False,
                    "pin_status": "not_attempted",
                    "unchanged": False, **self._api_error(exc),
                }

            # Save content success BEFORE trying pin; pin rejection cannot roll it back.
            self.store.save_summary(destination, key, message_id, digest,
                                    bool(existing["pinned"]) if existing else False)
            if pin and not (existing and existing["pinned"]):
                result = self._pin_summary_locked(destination, key)
                result["unchanged"] = False
                return result
            return {"status": "accepted", "message_id": message_id,
                    "content_saved": True,
                    "pin_status": "pinned" if existing and existing["pinned"] else "not_requested",
                    "unchanged": False}
