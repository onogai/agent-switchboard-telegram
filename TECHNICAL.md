# Telegram Service (outbound core)

Independent Telegram add-on, backed by private SQLite and encrypted bot tokens. No import of Gateway or Task Queue code. This service is **not** the active legacy sender until a tested cutover.

- Internal MCP at `http://telegram-delivery:8781/mcp/`; published only via an explicit Gateway upstream registration/grants.
- Private management API under `/internal/*`, requiring `Authorization: Bearer <TELEGRAM_ADMIN_TOKEN>` with a secret of at least 32 characters.
- `/health` for container liveness. No host port is published.
- Storage at `TELEGRAM_DB_PATH`, default `/data/telegram.sqlite3`.
- `TELEGRAM_ENCRYPTION_KEY` must be a Fernet key; keep it outside the repository and back it up separately.
- `TELEGRAM_ADMIN_TOKEN` must be a random 32+ character secret.

Generate a Fernet key once: `python3 -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())'` after installing `cryptography`. Do not rotate it without re-encrypting all stored tokens.

Start independently: `docker compose --profile telegram up -d --build telegram-delivery` after setting both secrets and creating persistent `state/telegram` writable by container user 10001. Initial registration is through Infrastructure Admin's Telegram Delivery panel (the private management API remains available for troubleshooting). Configure `TELEGRAM_ADMIN_TOKEN` in the existing Admin Compose service and run the optional Telegram Compose profile. Do not enable this service before credentials and volume permissions are ready.

The current `gateway/telegram.py` legacy sender and old Admin test button remain unchanged. New sender requires configuring bots/destinations first. It is not registered or granted in Gateway automatically.

Tests: `python3 -m unittest discover -s tests -p 'test_telegram_service.py'` (requires `telegram_service/requirements.txt`). CI runs without production credentials, making no Telegram network calls.

**Current limitations:** no Inbox/Reply, no chat discovery, no history retention policies, no bot token rotation, no Queue integration. Delivery status distinguishes accepted/failed/unknown/partial. A timeout after an outbound API request may have delivered the message: unknown outcomes must not be automatically resent.

## Targeted deployment (preferred)

From the laptop checkout after merging/pulling the desired commit:

```bash
./deploy-telegram.sh --dry-run --with-admin
./deploy-telegram.sh --with-admin
```

This syncs only Telegram Hub, Compose and (optionally) Admin UI/backend source, then rebuilds and recreates **only** `telegram-delivery` and `infrastructure_admin`. Without `--with-admin`, only Telegram Hub is recreated. The script reads existing deployment credentials from the remote `.env` and does not synchronize remote secrets or `state/`. It requires the existing remote Telegram installation; initial secrets and SQLite volume setup still follow the instructions above.

Admin uses the separate `nogai-infrastructure-admin:homelab` image tag (built from the same root Dockerfile); thus targeted Admin deployment never overwrites the Gateway/Queue image tag. The regular full `deploy.sh` maintains that Admin tag automatically.

If a targeted build or health check fails, inspect the logs and repair or roll back explicitly; the script does not automatically revert images or source files. No additional VPS reverse proxy changes are needed.

## Summary pin diagnostics and partial success

On upsert, successful send/edit is saved in SQLite *before* pin. Pin rejection returns `status: partial`, `content_saved: true`, `pin_status: failed`, `retry_scope: pin_only`, original `message_id`, numeric Telegram `error_code` and sanitized `description`. A timeout returns `pin_status: unknown`. **Task Queue must not rerun the digest if content_saved=true**; handle pin independently.

Admin → Telegram Hub → Activity provides Summary status (read only), Bot rights (getChatMember) and **Retry pin only** on a stored key; no new message or editing. Private Admin endpoints: POST `/internal/summary/status`, `/internal/summary/permissions` with `{destination,key}`, and `/internal/summary/pin` with `{destination,key,confirm:true}`. They require Admin authentication and CSRF. Nothing is exposed to agents as a new MCP tool.

For group/supergroup pinning, an admin bot typically needs `can_pin_messages`; channel pinning uses `can_edit_messages`. Telegram API already supports `disable_notification:true` (suppresses pin notifications, not necessarily a visible service event). Successful pin persists `pinned=true`, making future retries no-ops. An uncertain prior pin is checked via getChat().pinned_message before retrying; the latest pinned message cannot prove all pins, so a Telegram 'already pinned' rejection is also considered success. The existing SQLite table receives additive diagnostic columns; old rows remain readable.
