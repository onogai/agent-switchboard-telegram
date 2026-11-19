# Telegram Delivery production

Telegram owns the independent `switchboard-telegram` Compose project, with
one container `switchboard-telegram-delivery` and image tag
`switchboard-telegram:production`. Gateway and Manager use the internal
Docker DNS alias `telegram-delivery:8781` on an existing private network.

## Private host configuration

`.env.production` is local and must not be committed. It contains **paths**
and the image tag, not the Telegram bot tokens:

```dotenv
TELEGRAM_ENV_FILE=/home/onogai/service-data/secrets/telegram.env
TELEGRAM_STATE_DIR=/home/onogai/service-data/telegram
TELEGRAM_GATEWAY_NETWORK=<existing shared private network>
TELEGRAM_IMAGE=switchboard-telegram:production
```

The runtime env referenced by `TELEGRAM_ENV_FILE` holds a persistent Fernet
`TELEGRAM_ENCRYPTION_KEY` and `TELEGRAM_ADMIN_TOKEN`. It is root-readable
only. SQLite state stays writable to UID 10001.

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

For a deliberate image rebuild, back up SQLite and credentials first,
then build `switchboard-telegram:production` from this repository's
Dockerfile. Validate against `/health` and a read-only MCP
`list_destinations` call.

Never start a second Telegram receiver for the same bot, mount production
data into staging `compose.yaml`, rotate a Fernet key independently of
encrypted SQLite, run `down -v` or print private Compose configuration.

Consistent SQLite/WAL backups and the matching runtime secrets are included
in the host's `agent-switchboard/scripts/backup-production.py` workflow.
The original 2026 migration instructions and scripts remain in Git history.
