"""Minimal Telegram update receiver: discover chats, never ingest/store message bodies."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from .bot_api import BotApiError
from .service import TelegramDelivery

_TYPES = {"private", "group", "supergroup", "channel"}
_MEMBER = {"member", "administrator", "creator"}
_REGISTER = re.compile(r"^/register(?:@([a-zA-Z0-9_]+))?(?:\s|$)", re.IGNORECASE)


def parse_chat_update(update: dict, bot_id: str, bot_username: str) -> dict | None:
    """Return only minimal chat metadata. Unrelated messages are not persisted."""
    source = None
    present = True
    chat = None
    membership = update.get("my_chat_member")
    if isinstance(membership, dict):
        member = membership.get("new_chat_member", {})
        user = member.get("user", {}) if isinstance(member, dict) else {}
        if str(user.get("id", "")) != bot_id:
            return None
        chat = membership.get("chat")
        state = member.get("status")
        present = state in _MEMBER or (
            state == "restricted" and member.get("is_member") is True)
        source = "membership"
    else:
        message = update.get("message") or update.get("channel_post")
        if not isinstance(message, dict):
            return None
        chat = message.get("chat")
        raw_text = message.get("text")
        match = _REGISTER.match(raw_text) if isinstance(raw_text, str) else None
        if not match:
            return None
        name = match.group(1)
        if name and name.lower() != bot_username.lower():
            return None
        if not name and (not isinstance(chat, dict) or chat.get("type") != "private"):
            return None  # groups/channels must address the command to this bot
        source = "command"

    if not isinstance(chat, dict):
        return None
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    if type(chat_id) is not int or chat_type not in _TYPES:
        return None
    title = chat.get("title") or chat.get("first_name") or str(chat_id)
    if not isinstance(title, str):
        title = str(chat_id)
    return {
        "chat_id": str(chat_id), "chat_type": chat_type,
        "title": title.strip()[:120] or str(chat_id),
        "source": source, "present": present,
    }


class ChatDiscovery:
    """One receiver inside the Telegram container; no public HTTP routes or agents."""
    def __init__(self, service: TelegramDelivery):
        self.service = service
        self.states: dict[str, dict] = {}

    def poll_bot(self, bot: dict) -> int:
        bot_id = bot["id"]
        try:
            token = self.service.store.bot_token(bot_id)
            info = self.service.api.call(token, "getWebhookInfo")
            if not isinstance(info, dict):
                raise BotApiError("telegram_invalid_webhook_info")
            if info.get("url"):
                self.states[bot_id] = {"state": "webhook_conflict",
                                       "detail": "Telegram webhook already configured"}
                return 0
            payload = {"timeout": 5, "limit": 100,
                       "allowed_updates": ["my_chat_member", "message", "channel_post"]}
            offset = self.service.store.discovery_offset(bot_id)
            if offset is not None:
                payload["offset"] = offset
            updates = self.service.api.call(token, "getUpdates", payload)
            if not isinstance(updates, list):
                raise BotApiError("telegram_invalid_updates")
            count = 0
            for update in updates:
                if not isinstance(update, dict) or type(update.get("update_id")) is not int:
                    raise BotApiError("telegram_invalid_updates")
                event = parse_chat_update(update, bot_id, bot["username"])
                self.service.store.record_discovery_update(bot_id, update["update_id"], event)
                count += int(event is not None)
            self.states[bot_id] = {"state": "polling",
                                   "checked_at": datetime.now(timezone.utc).isoformat()}
            return count
        except BotApiError as exc:
            self.states[bot_id] = {"state": "receiver_conflict" if exc.code == "telegram_http_409" else "error",
                                   "detail": exc.code}
            return 0
        except Exception:
            self.states[bot_id] = {"state": "error", "detail": "receiver_unavailable"}
            return 0

    def poll_once(self) -> int:
        return sum(self.poll_bot(bot) for bot in self.service.store.list_bots()
                   if bot["enabled"])
