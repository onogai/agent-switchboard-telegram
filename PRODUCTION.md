# Production operations

Telegram Delivery owns one independent Docker Compose project,
`switchboard-telegram`, with one container, `switchboard-telegram-delivery`,
and one image tag, `switchboard-telegram:production`.

Gateway and Manager continue to reach it through the **unchanged** private
Docker DNS alias `telegram-delivery:8781`. The existing external Docker
network is deliberately not renamed: other live services use it.

## One-time rename from the previous production deployment

After pulling this repository, run:

```bash
cd ~/agent-switchboard-telegram
git pull --ff-only
python3 scripts/rename_production_service.py
```

This one-time command:

- Validates the existing Compose owner, running container, SQLite bind mount,
  private runtime env and external network.
- Retags the **same image ID** without rebuilding.
- Changes only `TELEGRAM_IMAGE` in the local `.env.production`.
- Stops the previous receiver before creating the new one, so only one
  process can poll Telegram.
- Archives a stopped-state copy of the SQLite directory and private runtime
  settings under `~/switchboard-cutover-backups/`.
- Starts the new Compose owner, waits for health and verifies image identity,
  state path, credentials, private DNS and Core connectivity.
- Restarts the original receiver and restores `.env.production` if cutover
  fails. The original container is removed **only after** verification.

No bot token, encryption key, or SQLite database is rotated or recreated.
Do not manually run the new Compose service alongside the previous receiver.

## Private host configuration

`.env.production` is not committed to Git. It retains the current real
paths and existing external network name, for example:

```dotenv
TELEGRAM_ENV_FILE=/home/onogai/service-data/secrets/telegram.env
TELEGRAM_STATE_DIR=/home/onogai/service-data/telegram
TELEGRAM_GATEWAY_NETWORK=<existing external Docker network>
TELEGRAM_IMAGE=switchboard-telegram:production
```

The referenced runtime env contains private `TELEGRAM_ENCRYPTION_KEY`
and `TELEGRAM_ADMIN_TOKEN`; it must remain root-readable only. SQLite must
remain writable by UID 10001.

## Routine operations

```bash
cd ~/agent-switchboard-telegram
sudo docker compose -p switchboard-telegram \
  --env-file .env.production -f compose.production.yaml config --quiet

sudo docker compose -p switchboard-telegram \
  --env-file .env.production -f compose.production.yaml ps

sudo docker compose -p switchboard-telegram \
  --env-file .env.production -f compose.production.yaml up -d --no-build telegram-delivery
```

Never run the staging `compose.yaml` against production state, run a
second Telegram receiver, rotate the Fernet key, or use `down -v`.
Keep SQLite snapshots and matching runtime credentials together.

After the rename, update `LOGGING_COMPOSE_PROJECTS` in the independent
Logging checkout to include `switchboard-telegram` instead of the previous
Telegram project, then rerun `./start-on-host.sh` there.

The earlier [PRODUCTION_CUTOVER.md](PRODUCTION_CUTOVER.md) records the
historical initial ownership cutover and should not be used for routine
operations.
