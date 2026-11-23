# Telegram Delivery — architecture and operations

## Ownership and interfaces

This repository owns the Telegram process, bot API client, encrypted bot token
storage, chat discovery, named destinations, delivery records and summary
management. Gateway Core owns OAuth, source configuration, public tool
contracts and grants. Manager owns the browser PIN/session/CSRF and proxies
the private Telegram administration API. Task Queue can send Telegram
messages through the registered MCP tools; it does not read Telegram SQLite.

- MCP endpoint: `http://telegram-delivery:8781/mcp/` (private only).
- Management: `/internal/*`, `Authorization: Bearer
  <TELEGRAM_ADMIN_TOKEN>` with a 32+ character secret.
- Liveness: `/health`; no public host ports.
- Persistent state: `TELEGRAM_DB_PATH` (normally
  `/data/telegram.sqlite3`).
- Encryption: `TELEGRAM_ENCRYPTION_KEY`, a stable Fernet key. Do not rotate
  it without explicitly re-encrypting stored bot credentials.

Generate a fresh Fernet key for **new** installations only:
`python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'`
(with `cryptography` installed). Production must preserve its existing key.

## Managed add-on migration path

The repo also ships an isolated schema-v2 Compose/manifest and a guarded offline
`scripts/adopt_existing.py` importer. It preserves the real legacy bot-token
encryption key, Admin token, stored IDs, idempotency records and summaries.
The new managed instance has no discovery polling until its
`state/database/discovery.enabled` gate file exists. The old and new SQLite
databases must never run as concurrent delivery owners. Manager's existing
legacy URL must be repointed at `http://addon-telegramhub:8781` at cutover;
the Switchboard Core production Compose now supports the
`SWITCHBOARD_TELEGRAM_SERVICE_URL` override.

For the exact staged install, freeze, backup, import, cutover, Manager routing,
verification and rollback procedure see [MIGRATION.md](MIGRATION.md).
**Repository support does not imply that production has been migrated.**

## Runtime behavior

The HTTP MCP exports `list_destinations`, `telegram_send`,
`telegram_upsert_summary` and `send_telegram`. The last tool is a
compatibility shim using `TELEGRAM_DEFAULT_DESTINATION` and should not be
mistaken for the removed legacy Gateway source. The modern production
source is `telegramhub`; grants and stable names are stored in Gateway.

The service polls Telegram getUpdates for discovery when no conflicting
webhook exists. It records chat identity and presence, **not chat message
bodies**. Management supports discovered-chat review, ignoring, approving,
destination impact reports, guarded rebind/delete and bot toggles.

`telegram_send` accepts an optional idempotency key. Delivery outcomes can
be accepted, failed, unknown or partial; a timeout after a Telegram API
request can mean the message was delivered. An unknown outcome must not
trigger a blind resend. Keys are scoped to destination bindings to avoid
reuse across a rebind.

`telegram_upsert_summary` stores one message per destination/key,
updates its content and can pin it. Content persistence and pin result are
separate: a pin rejection can return `status: partial` while
`content_saved: true`. Do **not** resend or regenerate the summary in that
case. Manager exposes `/internal/summary/status`,
`/internal/summary/permissions` and `/internal/summary/pin` for
diagnosing rights and retrying **pin only**. The API requests require the
private service token; Manager also enforces its own browser CSRF/session.

## Formatting

Modern `telegram_send` and `telegram_upsert_summary` tools accept optional
`parse_mode="HTML"` (omitted means backwards-compatible plain text).
Formatting is included in delivery idempotency and summary change detection.
Long HTML sends split at visible text boundaries with balanced tags;
malformed HTML is rejected before the first API call. Telegram can still
reject unsupported attributes or invalid links; errors are reported rather
than blindly retried. HTML-escape untrusted text at the source.

The legacy `send_telegram` compatibility tool remains plain text.

## Current production and deployment boundary

As of 18 November 2026, Telegram is already running independently with its
existing image and original SQLite path under the Switchboard production
root. The Core/Queue modular cutover deliberately **did not** rebuild or
replace the Telegram process. Core's `compose.yaml` contains an optional
Telegram service profile that should remain inert until a targeted
Telegram cutover is planned.

A Git merge or green test run is **not** a deployment. The standalone
`compose.yaml` in this repository is for isolated staging; do not launch
it using the production project name/state, and never run global Compose
up/down or `--remove-orphans` on the live stack. The historical
`deploy-telegram.sh` instructions from the retired monorepo are obsolete.

### Targeted production cutover checklist (not yet performed)

1. Record the currently running Telegram container ID, image digest,
   configuration and private networks, as well as its Docker Compose owner.
2. Verify whether the new image has the same `/mcp/`, `/internal/*`,
   health and tool contracts as the running service; use isolated tests and
   never probe by sending a live message.
3. Check SQLite migration with the offline preflight below. It uses SQLite's
   online backup API to create a consistent **temporary copy**, validates the
   encryption key, applies current schema migrations only to the copy, and
   compares pre-/post-migration protected row data. Retain a separate durable
   matched backup (including rollback image/config and the original key).
4. Verify the service's container UID/GID can access the existing state,
   and preserve the exact original `TELEGRAM_ENCRYPTION_KEY`,
   `TELEGRAM_ADMIN_TOKEN`, mounted paths, DNS name and network aliases.
5. Validate a selected-service Compose config and rollback plan; schedule
   a single controlled container replacement. Do not recreate Core,
   Manager, Queue or unrelated upstreams.
6. After cutover, verify /health, source discovery and public contracts,
   management API through Manager, Telegram discovery receiver status and
   saved destinations. Exercise real sending only with explicit approval.
7. Keep the matched old image/config and SQLite backup until explicitly
   retiring rollback.

## Testing

Run `bash scripts/test.sh`. It builds an isolated image and runs
`tests/test_*.py` with network disabled and no mounted production state.
This validates outbound, management and regression behavior without live
Telegram access.


## Read-only database migration preflight

From the **Telegram repository checkout on the Docker host**, with the existing
key supplied through the private environment (never print or paste the key):

```bash
# Run with permission to read the existing SQLite and its WAL:
python3 scripts/preflight_sqlite.py \
  --db /home/onogai/nogai-mcp-homelab/state/telegram/telegram.sqlite3
```

This reads the specified database with SQLite `mode=ro`, creates an online
consistent snapshot in a private temporary directory, then runs the current
`Store.initialize()` migration **only on that snapshot**. It verifies SQLite
integrity, decrypts every bot token (without printing it), and checks that
protected historical records and row counts survive. The temporary copy is
deleted at exit. It does not contact Telegram, send a message, start Docker
services or change the production DB.

The preflight intentionally does **not** prove Docker image parity,
network/DNS compatibility, Manager proxy routing or the live container's
mounts/UID. Confirm those separately before a targeted deployment. Do not
display environment variables containing encryption keys or service tokens.
