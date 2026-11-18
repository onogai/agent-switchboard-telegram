#!/usr/bin/env python3
"""Create a private Telegram-only env file from an existing Docker container.

Explicit operator action for production ownership migration. Does not print
secrets, modify the source container, change SQLite or start services.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

FIELDS = ("TELEGRAM_ENCRYPTION_KEY", "TELEGRAM_ADMIN_TOKEN")
OPTIONAL = ("TELEGRAM_DEFAULT_DESTINATION",)


def get_container_env(name: str) -> dict[str, str]:
    data = json.loads(subprocess.check_output(
        ["docker", "inspect", name], text=True,
        stderr=subprocess.DEVNULL,
    ))
    if len(data) != 1:
        raise RuntimeError("Exactly one source container must exist")
    result = {}
    for item in data[0]["Config"].get("Env", []):
        key, separator, value = item.partition("=")
        if separator and key in (*FIELDS, *OPTIONAL):
            result[key] = value
    if not result.get(FIELDS[0]):
        raise RuntimeError("Source container has no Telegram encryption key")
    if len(result.get(FIELDS[1], "")) < 32:
        raise RuntimeError("Source container has no valid Telegram admin token")
    if any("\n" in value or "\r" in value for value in result.values()):
        raise RuntimeError("Invalid multiline Docker environment value")
    return result


def save_env(dest: Path, env: dict[str, str]) -> None:
    dest = dest.expanduser().absolute()
    if not dest.parent.is_dir():
        raise RuntimeError("Output parent directory must already exist")
    # Refuse to replace an existing file or follow a final path symlink.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(dest, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            for key in (*FIELDS, *OPTIONAL):
                if key in env:
                    out.write(f"{key}={env[key]}\n")
            out.flush()
            os.fsync(out.fileno())
        os.chmod(dest, stat.S_IRUSR | stat.S_IWUSR)
    except Exception:
        dest.unlink(missing_ok=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", default="nogai-telegram-delivery")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        env = get_container_env(args.container)
        save_env(args.output, env)
    except Exception as exc:
        print("FAIL private Telegram env export: " + type(exc).__name__, file=sys.stderr)
        return 1
    print("PASS private Telegram env export (mode 0600; secrets not displayed)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
