# Switchboard Telegram Delivery

Independent Telegram add-on with its own SQLite state, encrypted bot tokens,
bot/chat discovery, named destinations, private management API and HTTP MCP.
No Gateway or Queue Python package is required.

## Endpoints and security

- Private MCP: `http://telegram-delivery:8781/mcp/`
- Private management API: `/internal/*`, authenticated by
  `TELEGRAM_ADMIN_TOKEN`
- Health: `/health`
- SQLite: `TELEGRAM_DB_PATH` (default `/data/telegram.sqlite3`)
- Bot token encryption: `TELEGRAM_ENCRYPTION_KEY` (stable Fernet key)

MCP tools: `list_destinations`, `telegram_send`,
`telegram_upsert_summary`, and optional compatibility alias
`send_telegram` (requires explicit `TELEGRAM_DEFAULT_DESTINATION`).
Modern Gateway uses the `telegramhub` Registry source; its public names,
OAuth, approvals and grants remain Core responsibilities.

## Formatted messages

`telegram_send` and `telegram_upsert_summary` accept optional
`parse_mode="HTML"`. When omitted, content stays literal plain text.
Use Telegram Bot API HTML markup: `<b>bold</b>`, `<i>italic</i>`,
`<a href="https://example.com">links</a>`, and `<code>code</code>`.
Escape external text with HTML entities (`&amp;`, `&lt;`).

Formatted sends split long messages at visible-text boundaries while
closing and reopening tags. Summaries must fit a single 4096-unit message.
Invalid markup and unsupported modes fail before delivery. Telegram may
reject unsupported attributes or invalid links. The legacy `send_telegram`
tool remains plain text only.

## Functionality

- Store bot credentials encrypted; enable/disable bots and discover chats
  through polling (a webhook may block polling).
- Approve chats, map them to destinations and manage rebindings.
- Idempotent outbound sends and uncertain-outcome diagnostics; never
  blindly retry a potentially accepted Telegram API send.
- Maintain a daily pinned summary and retry pinning without duplicating it.
- Manage bot/destination/delivery metadata from the private Manager proxy.

## Managed schema-v2 add-on

The repository now includes `switchboard-addon.yaml` and `compose.addon.yaml`
for installation as the isolated `telegramhub` add-on. Managed credentials are
file-mounted; a new instance uses a deterministic Fernet key derived from an
installer-generated random seed. Existing Fernet keys are preserved literally
during migration. Discovery requires a deliberate gate file in managed state,
so staging cannot accidentally become a second Telegram bot receiver.

**For an existing production Telegram, follow [MIGRATION.md](MIGRATION.md).**
Do not point a new add-on directly at the old live SQLite directory and do
not try to install over the existing Registry source without staging.

## Deployment

See [PRODUCTION.md](PRODUCTION.md) for the current independent Compose
deployment, state ownership and commands. Staging `compose.yaml` uses
a disposable `./state` directory and is not production.

Run `bash scripts/test.sh` for disposable Docker tests and Compose
validation. Never expose MCP or the private management API to the
public Internet or start simultaneous Telegram receivers for one bot.
