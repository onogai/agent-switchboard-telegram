#!/usr/bin/env python3
"""Validate schema-v2 Telegram Compose using only disposable paths and fake keys."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    with TemporaryDirectory(prefix="telegram-addon-compose-") as tmp:
        state = Path(tmp) / "state"
        (state / "database").mkdir(parents=True)
        (state / "secrets").mkdir()
        for secret in ("encryptionkey", "admintoken"):
            (state / "secrets" / secret).write_text("not-a-real-secret\n")
        env = {**os.environ, "SWITCHBOARD_ADDON_INSTANCE": "telegramhub",
               "SWITCHBOARD_ADDON_STATE": str(state),
               "SWITCHBOARD_ADDONS_NETWORK": "switchboard-test-private",
               "HOST_UID": "10001", "HOST_GID": "10001"}
        result = subprocess.run(
            ["docker", "compose", "-p", "switchboard-addon-telegramhub",
             "-f", str(ROOT / "compose.addon.yaml"), "config", "--format", "json"],
            env=env, text=True, capture_output=True, check=True)
        value = json.loads(result.stdout)
        assert set(value["services"]) == {"telegramhub"}
        svc = value["services"]["telegramhub"]
        assert svc["user"] == "10001:10001"
        assert svc["read_only"] is True
        assert svc["cap_drop"] == ["ALL"]
        assert "no-new-privileges:true" in svc["security_opt"]
        assert "ports" not in svc and "privileged" not in svc
        assert set(svc["networks"]) == {"addons"}
        assert svc["networks"]["addons"]["aliases"] == ["addon-telegramhub"]
        assert value["networks"]["addons"]["external"] is True
        assert value["networks"]["addons"]["name"] == "switchboard-test-private"
        assert svc["environment"]["TELEGRAM_DISCOVERY_GATE_FILE"] == "/data/discovery.enabled"
        assert svc["environment"]["TELEGRAM_DB_PATH"] == "/data/telegram.sqlite3"
        assert len(svc["volumes"]) == 3
        mounts = {m["target"]: m for m in svc["volumes"]}
        assert mounts["/data"]["source"] == str(state / "database")
        assert mounts["/data"].get("read_only", False) is False
        for name in ("encryptionkey", "admintoken"):
            m = mounts["/run/addon-secrets/" + name]
            assert m["source"] == str(state / "secrets" / name)
            assert m["read_only"] is True
        print("PASS managed Telegram v2 Compose isolation, secrets, state and gated discovery")


if __name__ == "__main__":
    main()
