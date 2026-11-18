# Switchboard Telegram Delivery

Independent Telegram Delivery add-on with its own SQLite state, encrypted bot
tokens, bot/chat discovery, named destinations and private HTTP management API.
The same image exposes MCP tools for sending messages and maintaining a daily
pinned summary. No Gateway or Task Queue Python packages are required.

## Architecture

- HTTP MCP: `http://telegram-delivery:8781/mcp/` on a **private** Docker network.
- Private management API: `/internal/*`, authenticated by a strong
  `TELEGRAM_ADMIN_TOKEN`.
- Health probe: `/health`.
- SQLite: `TELEGRAM_DB_PATH` (default `/data/telegram.sqlite3`).
- Token encryption: `TELEGRAM_ENCRYPTION_KEY` (stable Fernet key).
- Docker image: built from this repository's `Dockerfile`.

MCP tools: `list_destinations`, `telegram_send`,
`telegram_upsert_summary`, and optional legacy-compatible
`send_telegram`. The legacy tool requires an explicitly configured
`TELEGRAM_DEFAULT_DESTINATION`; it is not automatically registered or
granted by Gateway.

The modern Gateway uses the `telegramhub` source, not its removed
`telegram/send_telegram` implementation. Public MCP names, approvals,
grants and OAuth remain Gateway responsibilities.

## Features

- Register encrypted bot credentials; enable/disable bots.
- Poll Telegram updates for chat discovery, without storing message bodies.
  A configured webhook can block polling; the receiver reports this state.
- Approve discovered chats and map them to named destinations; safely rebind
  or delete destinations with binding checks and archived summary records.
- Idempotent outbound delivery keyed to a destination binding; distinguish
  accepted, failed, unknown and partial outcomes. Do not blindly retry an
  uncertain Telegram API send.
- Create/edit a summary under a stable key, optionally pin it.
  Pin failures can be retried independently without resending the summary.
- Manage bots, destinations, discovered chats, deliveries and summary pin
  diagnostics through the private API used by Switchboard Manager.

## Production status

The modular Core/Task Queue cutover completed on **18 November 2026**.
Telegram Delivery is already running as an independent service using its
existing image and persistent SQLite directory. **Its separate repository
has not yet taken ownership of production deployment.** That is a
separate, targeted cutover with image and database compatibility checks.

The Core production Compose intentionally keeps Telegram under an optional,
normally inactive profile. Do not use this repository's staging Compose to
recreate the live Telegram container, mount live state during tests, or
perform global `docker compose up/down --remove-orphans`.

See [TECHNICAL.md](TECHNICAL.md) for security and data preservation.
For the independent production deployment manifest and guarded handoff,
see [PRODUCTION_CUTOVER.md](PRODUCTION_CUTOVER.md). Do not run the cutover
section without explicit operator approval.

## Verification

Run `bash scripts/test.sh` to build and test a disposable isolated image.
CI runs the same script without a Telegram bot, live token or production DB.
The staging `compose.yaml` uses its own `./state` and the private addons
network; it is **not** the production deployment manifest.
