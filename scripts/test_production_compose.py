#!/usr/bin/env python3
"""Static, secret-redacting validation of independent production Compose.

Uses fake credentials and a disposable directory. Does not create/start Docker
containers, connect to Telegram or access the real production SQLite.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "compose.production.yaml"


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="telegram-compose-test-") as tmp:
        root = Path(tmp)
        env_file = root / "telegram.env"
        env_file.write_text("TELEGRAM_ENCRYPTION_KEY=test-key-not-real\n"
                            "TELEGRAM_ADMIN_TOKEN=test-token-not-real\n")
        state = root / "state"
        state.mkdir()
        env = {
            **os.environ,
            "TELEGRAM_ENV_FILE": str(env_file),
            "TELEGRAM_STATE_DIR": str(state),
            "TELEGRAM_GATEWAY_NETWORK": "switchboard-test-primary",
            "TELEGRAM_IMAGE": "switchboard-telegram:production",
        }
        result = subprocess.run(
            ["docker", "compose", "-p", "switchboard-telegram",
             "-f", str(COMPOSE), "config", "--format", "json"],
            env=env, capture_output=True, text=True, check=True,
        )
        config = json.loads(result.stdout)
        services = config["services"]
        assert set(services) == {"telegram-delivery"}, "unexpected service ownership"
        svc = services["telegram-delivery"]
        assert config["name"] == "switchboard-telegram"
        assert svc["container_name"] == "switchboard-telegram-delivery"
        assert svc["image"] == "switchboard-telegram:production"
        assert svc["user"] == "10001:10001"
        assert svc["read_only"] is True
        assert "ports" not in svc and "expose" not in svc
        assert svc["environment"]["TELEGRAM_DB_PATH"] == "/data/telegram.sqlite3"
        assert svc["environment"]["TELEGRAM_ADMIN_TOKEN"] == "test-token-not-real"
        assert svc["environment"]["TELEGRAM_ENCRYPTION_KEY"] == "test-key-not-real"
        assert svc["networks"]["gateway"]["aliases"] == ["telegram-delivery"]
        assert config["networks"]["gateway"]["name"] == "switchboard-test-primary"
        assert config["networks"]["gateway"]["external"] is True
        assert len(svc["volumes"]) == 1
        mount = svc["volumes"][0]
        assert mount["source"] == str(state)
        assert mount["target"] == "/data"
        assert svc["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in svc["security_opt"]
        assert "8781" in str(svc["healthcheck"]["test"])
        print("PASS Telegram independent production Compose shape, secrets boundary, network and state mounts")


if __name__ == "__main__":
    main()
