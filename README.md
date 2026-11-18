# Switchboard Telegram

Standalone Telegram Delivery + MCP HTTP add-on extracted from Switchboard. The application has a private management HTTP API and MCP upstream, with persistent Telegram destinations and discovery.

Build: `docker build -t switchboard-telegram .`. State is controlled by `TELEGRAM_DB_PATH` and `TELEGRAM_ENCRYPTION_KEY`; do not expose service or management endpoints publicly. Before cutover preserve existing database, encryption key, bot bindings, chat IDs and webhook/poll state.

**Migration pending:** Gateway must be reconfigured to route legacy `send_telegram` separately; the existing `telegramhub` public tools are not the same as the old single-message tool.
