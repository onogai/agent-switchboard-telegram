# Production migration: legacy Telegram → managed schema-v2 add-on

This procedure is for the **existing** standalone Telegram Hub with source ID
`telegramhub` and legacy Compose container `switchboard-telegram-delivery`.
It does **not** run on ordinary new Telegram installations. It uses the
Switchboard CLI's **staged replacement** instead of registering a second
source, so Registry tool IDs/grants and client endpoints can be preserved.

**Do not simply run `addon install` without `--stage-for-replacement`.**
There is already an active `telegramhub` Registry source.

## Preconditions

- Telegram add-on repository with `switchboard-addon.yaml` on its default
  branch; Switchboard Core version containing schema-v2 update/cutover and
  configurable `SWITCHBOARD_TELEGRAM_SERVICE_URL` in production Compose.
- Existing `telegramhub` source is an enabled, anonymous HTTP source with
  its existing grants; use Manager to verify before staging.
- Confirm the existing Telegram Compose container, current image digest,
  configured env file and SQLite path. Keep its old Compose configuration and
  matching credentials available for rollback. Do not paste secrets into chat,
  command arguments, Git or shell transcripts.
- Confirm the host's Python environment has `cryptography`, `sqlite3`,
  and access to both the legacy database and its private env file.
- A maintenance window to pause **all delivery producers**, including
  scheduled Queue workers. Otherwise a delivery may occur after the DB
  snapshot and before cutover, creating divergent idempotency and summary
  state across deployments.
- Never expose the new management API or MCP to public host ports.

## 1. Stage a *blank* isolated candidate

From the Switchboard Core checkout on the Docker host:

```bash
./switchboard addon install https://github.com/onogai/agent-switchboard-telegram.git \
  --name telegramhub --target production --stage-for-replacement
./switchboard addons
./switchboard addon logs telegramhub
./switchboard addon stop telegramhub
```

The isolated candidate uses `addon-telegramhub:8781` on the private
add-ons network. Its generated encryption seed/admin token create a separate
**empty** SQLite at `addons/telegramhub/state/database/telegram.sqlite3`.
The new instance cannot poll the bot until a gate file is created at
`addons/telegramhub/state/database/discovery.enabled`. Do not create it yet.
The legacy instance still owns production deliveries and polling.

## 2. Read-only preflight

Use the actual legacy paths discovered on your host; these are **examples**,
not defaults and not command-line secret values:

```bash
python3 addons/telegramhub/source/scripts/adopt_existing.py \
  --instance "$PWD/addons/telegramhub" \
  --legacy-db /path/to/old/telegram.sqlite3 \
  --legacy-env /path/to/legacy-private-telegram.env
```

The command verifies expected managed add-on metadata, checks the source
Docker container's project ownership and compares the supplied secrets against
the *actual* container environment without printing them. It makes disposable
SQLite online-backup copies, audits integrity/migrations, and decrypts every
stored bot credential. It does not stop containers, send to Telegram or write
production state.

If the legacy env file contains `TELEGRAM_ENCRYPTION_KEY` or
`TELEGRAM_ADMIN_TOKEN` in an unsupported format, correct the source
configuration before proceeding. Never rotate either key during adoption.

## 3. Freeze delivery, back up and import offline

Pause Telegram producers (including automated Queue workers) **before**
stopping legacy Telegram. Ensure no in-flight sends remain. Preserve an
independent backup of old credentials, image/config and SQLite outside the
new add-on. Stop **only** the old Telegram container:

```bash
docker stop switchboard-telegram-delivery
./switchboard addon stop telegramhub

python3 addons/telegramhub/source/scripts/adopt_existing.py \
  --instance "$PWD/addons/telegramhub" \
  --legacy-db /path/to/old/telegram.sqlite3 \
  --legacy-env /path/to/legacy-private-telegram.env \
  --apply
```

`--apply` refuses to proceed if either Telegram container is running or
if the managed database contains content. It creates a durable backup in
`addons/telegramhub/state/legacy-backup`, imports a consistent SQLite
snapshot, installs the original Fernet key and Admin bearer token into
private read-only-mounted secret files and writes a one-time adoption marker.
The legacy database, image, Compose and credential file remain untouched.

**Important:** after importing do not restart the old container unless
performing a coordinated rollback; the two independent databases must never
be active at once.

## 4. Validate and switch the MCP source

```bash
./switchboard addon start telegramhub
./switchboard addon logs telegramhub
./switchboard addon cutover telegramhub --dry-run
./switchboard addon cutover telegramhub
```

The new service is healthy, but its discovery gate is still CLOSED.
The cutover uses Switchboard's guarded `telegramhub` Registry rebind and
preserves tool IDs/grants; changed tool contracts enter Manager review.
Confirm all three current modern tool names remain advertised and review any
changed contracts. The compatibility `send_telegram` tool may also appear
in the candidate catalog; do not grant it unnecessarily.

## 5. Switch Manager routing and activate discovery

In the **Core** production `.env.production`, set the Compose interpolation
variable (do not put this into the Telegram credentials file):

```dotenv
SWITCHBOARD_TELEGRAM_SERVICE_URL=http://addon-telegramhub:8781
```

Recreate **only** `gateway_manager` through the host's existing production
Compose adapter, e.g. `./switchboard production up -d --no-deps gateway_manager`
when that wrapper accepts normal Compose arguments. This does not rebuild Core
or the Task Queue. Verify the PIN-protected Telegram Manager panel reaches
the migrated destinations, and verify MCP source `telegramhub` is online.

When the legacy Telegram container is still stopped, allow the managed bot
to receive discovery updates without recreating its container:

```bash
touch addons/telegramhub/state/database/discovery.enabled
```

The new process checks this file before each polling cycle; an absent file
is a deliberate `polling disabled` state, not a delivery failure.

Verify the destination inventory, management authorization, stored summary
IDs/pins, and MCP tool contracts. Only then resume Queue workers. Any real
Telegram test send requires explicit approval; avoid an unnecessary
duplicate notification during migration.

## 6. Future upgrades

Once `installation.json` no longer has `staged_replacement` and the
Registry endpoint is `http://addon-telegramhub:8781/mcp/`:

```bash
./switchboard addon update telegramhub --dry-run
./switchboard addon update telegramhub
```

Updates retain `state/`, the original encryption key, admin token,
the discovery gate and all Registry grants. No standalone Telegram rebuild
command is needed.

## Rollback boundary

**Before the first managed send or summary update**, a guarded rollback is
possible: pause producers, stop the managed add-on (this also stops its
poller), start the original legacy container, run
`./switchboard addon rollback telegramhub --dry-run` and
`./switchboard addon rollback telegramhub`, then revert the Manager
`SWITCHBOARD_TELEGRAM_SERVICE_URL` override and recreate **only** Manager.
Reapprove tool contracts if required, verify functionality, resume producers.

**After the first managed write, do not simply restart the old Telegram DB.**
It no longer knows the newly accepted delivery IDs, idempotency keys or pinned
summary state. A rollback then requires a *new offline data reconciliation*
from the managed SQLite back to legacy before resuming delivery. Preserve
both state copies and stop writers while investigating. Automatic CLI
rollback switches the MCP endpoint only; it does not roll back SQLite or
change Manager routing.

Never run `docker compose down -v`, `--remove-orphans`, global production
Compose up/down, or any manual Docker command that recreates Core/Manager/Queue
as a side effect of this migration.
