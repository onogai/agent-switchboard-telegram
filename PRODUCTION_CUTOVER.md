# Targeted production ownership cutover — Telegram Delivery

**Do not execute the switch section until the operator explicitly approves the
short Telegram downtime.** This repository has its own independent production
Compose manifest. It must not recreate Gateway, Manager, Task Queue, or other
upstreams. Existing Core Compose still has an inactive historical Telegram
profile; never run that profile alongside the new owner.

## Verified host baseline (18 Nov 2026)

- Existing container: `nogai-telegram-delivery`, healthy, zero restarts.
- Original Compose project: `nogai-mcp-homelab`, service `telegram-delivery`.
- UID:GID: `10001:10001`.
- Data bind mount: `/home/onogai/nogai-mcp-homelab/state/telegram:/data`,
  read/write.
- Network: `nogai-mcp-homelab_default`, **not**
  `switchboard_addons`. Gateway uses DNS `telegram-delivery:8781`.
- No host port published. Fernet key and management bearer present inside
  original container; no legacy default destination.
- The SQLite offline migration/decryption preflight reported PASS.
- New image `nogai-telegram-delivery:cutover-candidate` built successfully
  and exports the four expected MCP tools.

Recheck these facts immediately before any switch. Do not dump Docker
environment variables, bot tokens or the private env file to the console.

## Phase A — safe preparation (no service interruption)

From the fresh `agent-switchboard-telegram` checkout on the Docker host:

```bash
cd /home/onogai/agent-switchboard-telegram
git pull --ff-only

# Run this only once. Creates a root-owned 0600 file; refuses overwrite.
# Contains only the existing Telegram encryption key and management token.
sudo python3 scripts/create_runtime_env.py \
  --output /home/onogai/nogai-mcp-homelab/telegram-delivery.runtime.env

# Confirm file mode without printing its contents.
sudo stat -c '%a %U:%G %n' \
  /home/onogai/nogai-mcp-homelab/telegram-delivery.runtime.env

# Set NON-secret Compose input paths and the already-built candidate image.
export TELEGRAM_ENV_FILE=/home/onogai/nogai-mcp-homelab/telegram-delivery.runtime.env
export TELEGRAM_STATE_DIR=/home/onogai/nogai-mcp-homelab/state/telegram
export TELEGRAM_GATEWAY_NETWORK=nogai-mcp-homelab_default
export TELEGRAM_IMAGE=nogai-telegram-delivery:cutover-candidate

# Confirm the existing private Docker network is present.
sudo docker network inspect "$TELEGRAM_GATEWAY_NETWORK" --format '{{.Name}}'

# Important: --quiet. NEVER print the full resolved production Compose:
# it would include the private service credentials.
sudo --preserve-env=TELEGRAM_ENV_FILE,TELEGRAM_STATE_DIR,TELEGRAM_GATEWAY_NETWORK,TELEGRAM_IMAGE \
  docker compose -p nogai-telegram-standalone \
  -f compose.production.yaml config --quiet

# Old and candidate images should exist, without running either image.
sudo docker image inspect nogai-telegram-delivery:homelab \
  nogai-telegram-delivery:cutover-candidate \
  --format '{{.Id}}'
```

The new manifest intentionally mounts the **existing** SQLite directory,
preserves UID and the `telegram-delivery` DNS alias on the **original**
Gateway network, exports no host ports, and owns only one service. No
`docker compose up/down` in Phase A.

## Phase B — consistent rollback checkpoint

Before stopping the old service, save an online SQLite backup with the Python
`sqlite3.Connection.backup` API. Do not `cp` a live SQLite database with
WAL. Restrict the backup directory to root and keep the previous Docker image
and configuration. Preserve the private runtime env file from Phase A.

Example (creates a new 0600 snapshot; leaves source untouched):

```bash
sudo install -d -m 0700 /home/onogai/nogai-mcp-homelab/telegram-cutover-backups
sudo python3 - <<'PY'
import os
from pathlib import Path
import sqlite3
from datetime import datetime, timezone
source = Path("/home/onogai/nogai-mcp-homelab/state/telegram/telegram.sqlite3")
directory = Path("/home/onogai/nogai-mcp-homelab/telegram-cutover-backups")
stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
target = directory / f"telegram-{stamp}.sqlite3"
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
os.close(fd)
try:
    with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as original:
        with sqlite3.connect(target) as backup:
            original.backup(backup)
            assert backup.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
except Exception:
    target.unlink(missing_ok=True)
    raise
print("PASS: consistent rollback SQLite checkpoint created (mode 0600)")
PY
```

Record the created backup filename privately. Do not overwrite the original DB
or restore any checkpoint while a container still has it open.

## Phase C — operator-approved switch only

Do **not** perform this phase without verifying Phase A/B results and
confirming a maintenance window. No two Telegram receivers should poll the
same bots concurrently. The original container is **stopped and renamed**,
not destroyed, so its exact image/config can be restarted.

```bash
# 1. Stop the old service and free its fixed container name.
sudo docker stop nogai-telegram-delivery
sudo docker rename nogai-telegram-delivery nogai-telegram-delivery-rollback

# 2. Create exactly ONE Telegram service in the NEW Compose project.
sudo --preserve-env=TELEGRAM_ENV_FILE,TELEGRAM_STATE_DIR,TELEGRAM_GATEWAY_NETWORK,TELEGRAM_IMAGE \
  docker compose -p nogai-telegram-standalone \
  -f compose.production.yaml up -d --no-deps --no-build telegram-delivery

# 3. Verify health and network ownership (no secrets).
sudo docker inspect nogai-telegram-delivery \
  --format '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}}'
sudo docker inspect nogai-telegram-delivery \
  --format '{{index .Config.Labels "com.docker.compose.project"}}'
```

Also verify the existing `telegramhub` MCP source through Gateway, both
approved destinations through `list_destinations`, and private Manager
API status. Do not send a live message without operator approval.

### Rollback if the new service fails

Keep the previous container and matched SQLite backup. Stop/remove **only**
the new Telegram service before recovering the original name; do not run
a global project `down` or `--remove-orphans`:

```bash
sudo --preserve-env=TELEGRAM_ENV_FILE,TELEGRAM_STATE_DIR,TELEGRAM_GATEWAY_NETWORK,TELEGRAM_IMAGE \
  docker compose -p nogai-telegram-standalone -f compose.production.yaml \
  stop telegram-delivery
sudo --preserve-env=TELEGRAM_ENV_FILE,TELEGRAM_STATE_DIR,TELEGRAM_GATEWAY_NETWORK,TELEGRAM_IMAGE \
  docker compose -p nogai-telegram-standalone -f compose.production.yaml \
  rm -f telegram-delivery
sudo docker rename nogai-telegram-delivery-rollback nogai-telegram-delivery
sudo docker start nogai-telegram-delivery
```

If the candidate has changed the live database incompatibly, first stop
**all** writers and perform a separately reviewed restore of the matching
checkpoint. Restoring SQLite may roll back recent Telegram delivery history,
so never restore blindly. Keep the old container and checkpoint until the
operator explicitly retires the rollback plan.

**After successful cutover**, update the Core repository to remove its
obsolete optional Telegram service definition; this is a separate source-only
change. The old Core Compose project must not subsequently manage this service.
