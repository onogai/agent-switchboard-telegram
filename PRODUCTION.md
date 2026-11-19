# Production operations

Telegram Delivery is deployed as an independent Compose project,
`nogai-telegram-standalone`. The service is reachable from Gateway/Manager
through `http://telegram-delivery:8781` on the existing private network.

The initial ownership cutover is complete. A subsequent host-directory
cleanup migrates the **same** encrypted SQLite state and private runtime
credentials outside the former monolithic checkout. It must not rotate the
Fernet key or management token.

## Local private Compose paths

The host's `.env.production` is not tracked by Git and contains paths,
the network name and the current image tag, but no secret values:

```dotenv
TELEGRAM_ENV_FILE=/home/onogai/service-data/secrets/telegram.env
TELEGRAM_STATE_DIR=/home/onogai/service-data/telegram
TELEGRAM_GATEWAY_NETWORK=nogai-mcp-homelab_default
TELEGRAM_IMAGE=nogai-telegram-delivery:cutover-candidate
```

The image tag is an example; preserve the actual image from the current
container. The file pointed to by `TELEGRAM_ENV_FILE` contains private
`TELEGRAM_ENCRYPTION_KEY` and `TELEGRAM_ADMIN_TOKEN`; it is root-readable
only. The SQLite directory must remain writable by the service's UID 10001.

The host migration script in the Core repository creates
`.env.production` after verifying the running service, and recreates
Telegram only after stopping the previous writer and copying all SQLite/WAL
files. Never copy only a live SQLite main file.

## Routine operations

Run on the host, from this repository:

```bash
sudo docker compose -p nogai-telegram-standalone \
  --env-file .env.production -f compose.production.yaml config --quiet

sudo docker compose -p nogai-telegram-standalone \
  --env-file .env.production -f compose.production.yaml ps

sudo docker compose -p nogai-telegram-standalone \
  --env-file .env.production -f compose.production.yaml up -d --no-build telegram-delivery
```

Do not run staging `compose.yaml` against production state, run a second
Telegram receiver, rotate the encryption key, or run `down -v`. Preserve
consistent SQLite snapshots and the matching runtime credentials together.

The earlier [PRODUCTION_CUTOVER.md](PRODUCTION_CUTOVER.md) documents the
initial ownership migration and its historical paths, not current
day-to-day commands.
