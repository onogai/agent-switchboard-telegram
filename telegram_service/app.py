"""Two distinct interfaces: private management HTTP and internal MCP upstream."""
from __future__ import annotations

import asyncio
import hmac
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .bot_api import BotApiError
from .discovery import ChatDiscovery
from .service import TelegramDelivery, units
from .store import DeliveryError, Store

_service: TelegramDelivery | None = None
_discovery: ChatDiscovery | None = None


def delivery_service() -> TelegramDelivery:
    global _service
    if _service is None:
        _service = TelegramDelivery(Store(
            Path(os.environ.get("TELEGRAM_DB_PATH", "/data/telegram.sqlite3")),
            os.environ.get("TELEGRAM_ENCRYPTION_KEY", ""),
        ))
    return _service


def discovery_receiver() -> ChatDiscovery:
    global _discovery
    service = delivery_service()
    if _discovery is None or _discovery.service is not service:
        _discovery = ChatDiscovery(service)
    return _discovery


async def poll_discovery_loop():
    while True:
        try:
            await asyncio.to_thread(discovery_receiver().poll_once)
        except Exception:
            pass  # Polling is best effort; outbound delivery is independent.
        await asyncio.sleep(5)


async def list_destinations() -> dict:
    """List enabled Telegram destinations preapproved in the private Admin."""
    return {"destinations": [
        {"slug": d["slug"], "label": d["label"], "chat_type": d["chat_type"]}
        for d in await asyncio.to_thread(delivery_service().store.destinations, True)
    ]}


async def telegram_send(destination: str, text: str, idempotency_key: str | None = None) -> dict:
    """Publish plain text only to an enabled named Telegram destination."""
    delivery = await asyncio.to_thread(delivery_service().send, destination, text, idempotency_key)
    # Physical chat identifiers belong to Admin history, not agent-facing MCP.
    return {key: value for key, value in delivery.items()
            if key not in {"bot_id", "chat_id", "logical_idempotency_key"}}


async def telegram_upsert_summary(destination: str, key: str, text: str,
                                  pin: bool = True) -> dict:
    """Create/update a daily summary, optionally pinning the existing message."""
    return await asyncio.to_thread(delivery_service().summary, destination, key, text, pin)


async def send_telegram(message: str) -> str:
    """Legacy Gateway-compatible one-message send to a configured default destination."""
    if not message or not message.strip():
        raise ValueError("Message must not be empty.")
    if len(message) > 4096 or units(message) > 4096:
        raise ValueError("Message must be at most 4096 characters / UTF-16 units.")
    destination = os.environ.get("TELEGRAM_DEFAULT_DESTINATION", "").strip()
    if not destination:
        raise RuntimeError("TELEGRAM_DEFAULT_DESTINATION must refer to an enabled destination.")
    delivery = await telegram_send(destination, message)
    if delivery.get("status") != "accepted" or len(delivery.get("message_ids", [])) != 1:
        raise RuntimeError("Telegram did not confirm a single accepted message.")
    return f"Telegram message sent (ID: {delivery['message_ids'][0]})."


mcp = FastMCP("Telegram Delivery")
mcp.tool(list_destinations, annotations=ToolAnnotations(
    read_only_hint=True, destructive_hint=False, open_world_hint=False))
mcp.tool(telegram_send, annotations=ToolAnnotations(
    read_only_hint=False, destructive_hint=True, open_world_hint=False))
mcp.tool(telegram_upsert_summary, annotations=ToolAnnotations(
    read_only_hint=False, destructive_hint=True, open_world_hint=False))
mcp.tool(send_telegram, annotations=ToolAnnotations(
    read_only_hint=False, destructive_hint=True, open_world_hint=False))


def response(value: dict, status: int = 200):
    return JSONResponse(value, status_code=status, headers={"Cache-Control": "no-store"})


def allowed(request) -> bool:
    secret = os.environ.get("TELEGRAM_ADMIN_TOKEN", "")
    header = request.headers.get("authorization", "")
    # Never allow empty credentials, even for loopback access.
    return (len(secret) >= 32 and
            hmac.compare_digest(header, "Bearer " + secret))


async def body(request):
    if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise DeliveryError("JSON content type required")
    if int(request.headers.get("content-length", "0") or "0") > 20000:
        raise DeliveryError("Request too large")
    value = await request.json()
    if not isinstance(value, dict):
        raise DeliveryError("JSON object required")
    return value


def management(handler):
    async def guarded(request):
        if not allowed(request):
            return response({"error": "Unauthorized"}, 401)
        try:
            return response(await handler(request))
        except DeliveryError as exc:
            return response({"error": str(exc)}, 400)
        except BotApiError as exc:
            return response({"error": exc.code,
                             "error_code": exc.error_code,
                             "description": exc.description}, 502)
        except (KeyError, TypeError, ValueError):
            return response({"error": "Invalid request"}, 400)
        except Exception:
            return response({"error": "Telegram operation failed"}, 503)
    return guarded


async def status(request):
    service = delivery_service()
    return {"configured": bool(service.store.list_bots()),
            "bots": len(service.store.list_bots()),
            "destinations": len(service.store.destinations())}


async def bots(request):
    return {"bots": await asyncio.to_thread(delivery_service().store.list_bots)}


async def add_bot(request):
    data = await body(request)
    if set(data) != {"token", "label"}:
        raise DeliveryError("token and label required")
    return await asyncio.to_thread(delivery_service().register_bot, data["token"], data["label"])


async def toggle_bot(request):
    data = await body(request)
    if set(data) != {"enabled"}:
        raise DeliveryError("enabled required")
    return await asyncio.to_thread(delivery_service().store.set_bot,
                                   request.path_params["bot_id"], data["enabled"])


async def discovered(request):
    service = delivery_service()
    return {"chats": await asyncio.to_thread(service.store.discovered_chats),
            "receivers": discovery_receiver().states}


async def ignore_discovered(request):
    data = await body(request)
    if set(data) != {"ignored"}:
        raise DeliveryError("ignored required")
    return await asyncio.to_thread(
        delivery_service().store.set_discovered_ignored,
        request.path_params["bot_id"], request.path_params["chat_id"], data["ignored"])


async def approve_discovered(request):
    data = await body(request)
    if set(data) != {"slug", "label"}:
        raise DeliveryError("slug and label required")
    bot_id = request.path_params["bot_id"]
    chat_id = request.path_params["chat_id"]
    candidate = delivery_service().store.discovered_chat(bot_id, chat_id)
    if candidate["ignored"] or not candidate["present"] or not candidate["bot_enabled"]:
        raise DeliveryError("Chat is unavailable for approval")
    # Telegram getChat verifies target and identity again at approval time.
    return await asyncio.to_thread(delivery_service().register_destination,
                                   data["slug"], bot_id, chat_id, data["label"])


async def destinations(request):
    return {"destinations": await asyncio.to_thread(delivery_service().store.destinations)}


async def add_destination(request):
    data = await body(request)
    if set(data) != {"slug", "bot_id", "chat_id", "label"}:
        raise DeliveryError("slug, bot_id, chat_id and label required")
    return await asyncio.to_thread(delivery_service().register_destination,
                                   data["slug"], data["bot_id"], data["chat_id"], data["label"])


async def destination_impact(request):
    return await asyncio.to_thread(delivery_service().store.destination_impact,
                                   request.path_params["slug"])


async def delete_destination(request):
    data = await body(request)
    if set(data) != {"expected_binding_id", "confirm"} or data["confirm"] is not True:
        raise DeliveryError("Explicit delete confirmation and binding version required")
    return await asyncio.to_thread(delivery_service().delete_destination,
                                   request.path_params["slug"], data["expected_binding_id"])


async def rebind_destination(request):
    data = await body(request)
    if set(data) != {"expected_binding_id", "bot_id", "chat_id", "label", "confirm"} or data["confirm"] is not True:
        raise DeliveryError("Explicit rebind confirmation and expected binding required")
    return await asyncio.to_thread(
        delivery_service().rebind_destination,
        request.path_params["slug"], data["expected_binding_id"],
        data["bot_id"], data["chat_id"], data["label"])


async def toggle_destination(request):
    data = await body(request)
    if set(data) != {"enabled"}:
        raise DeliveryError("enabled required")
    return await asyncio.to_thread(delivery_service().store.set_destination,
                                   request.path_params["slug"], data["enabled"])


async def test_send(request):
    data = await body(request)
    if set(data) != {"destination", "text", "confirm"} or data["confirm"] is not True:
        raise DeliveryError("Explicit test confirmation required")
    return await asyncio.to_thread(delivery_service().send, data["destination"], data["text"])


async def summary_status(request):
    data = await body(request)
    if set(data) != {"destination", "key"}:
        raise DeliveryError("destination and key required")
    return await asyncio.to_thread(delivery_service().summary_record,
                                   data["destination"], data["key"])


async def summary_permissions(request):
    data = await body(request)
    if set(data) != {"destination", "key"}:
        raise DeliveryError("destination and key required")
    # Do not probe arbitrary destinations or nonexistent summaries.
    await asyncio.to_thread(delivery_service().summary_record,
                            data["destination"], data["key"])
    return await asyncio.to_thread(delivery_service().pin_capability, data["destination"])


async def retry_summary_pin(request):
    data = await body(request)
    if set(data) != {"destination", "key", "confirm"} or data["confirm"] is not True:
        raise DeliveryError("Explicit pin confirmation required")
    return await asyncio.to_thread(delivery_service().pin_summary,
                                   data["destination"], data["key"])


async def deliveries(request):
    return {"deliveries": await asyncio.to_thread(delivery_service().store.recent, 50)}


async def health(request):
    return response({"status": "ok"})


management_routes = [
    Route("/health", health),
    Route("/internal/status", management(status)),
    Route("/internal/bots", management(bots)),
    Route("/internal/bots", management(add_bot), methods=["POST"]),
    Route("/internal/bots/{bot_id}", management(toggle_bot), methods=["PUT"]),
    Route("/internal/discovered", management(discovered)),
    Route("/internal/discovered/{bot_id}/{chat_id}", management(ignore_discovered), methods=["PUT"]),
    Route("/internal/discovered/{bot_id}/{chat_id}/approve", management(approve_discovered), methods=["POST"]),
    Route("/internal/destinations", management(destinations)),
    Route("/internal/destinations", management(add_destination), methods=["POST"]),
    Route("/internal/destinations/{slug}", management(toggle_destination), methods=["PUT"]),
    Route("/internal/destinations/{slug}/impact", management(destination_impact)),
    Route("/internal/destinations/{slug}/delete", management(delete_destination), methods=["POST"]),
    Route("/internal/destinations/{slug}/rebind", management(rebind_destination), methods=["POST"]),
    Route("/internal/test-send", management(test_send), methods=["POST"]),
    Route("/internal/deliveries", management(deliveries)),
    Route("/internal/summary/status", management(summary_status), methods=["POST"]),
    Route("/internal/summary/permissions", management(summary_permissions), methods=["POST"]),
    Route("/internal/summary/pin", management(retry_summary_pin), methods=["POST"]),
]

mcp_app = mcp.http_app(path="/")


@asynccontextmanager
async def lifespan(app):
    delivery_service()  # fail early if token encryption cannot be initialized
    async with mcp_app.lifespan(mcp_app):
        task = asyncio.create_task(poll_discovery_loop())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


app = Starlette(routes=[*management_routes, Mount("/mcp", app=mcp_app)],
                lifespan=lifespan)
