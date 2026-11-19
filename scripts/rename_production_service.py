#!/usr/bin/env python3
"""One-time, state-preserving production Telegram container/project rename.

Reuses the exact running image ID, SQLite directory, private credentials and
Gateway network. Stops the old receiver before starting the new one. On a
failed health/identity check, removes the new container and restarts the old.
Never prints secret environment values or resolved Compose configuration.
"""
from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[1]
ENV = ROOT / ".env.production"
COMPOSE = ROOT / "compose.production.yaml"
OLD = "nogai-telegram-delivery"
NEW = "switchboard-telegram-delivery"
PROJECT = "switchboard-telegram"
IMAGE = "switchboard-telegram:production"


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(args, cwd=ROOT, text=True, capture_output=True)
    if check and proc.returncode:
        raise RuntimeError("Command failed (output withheld to protect credentials): "
                           + " ".join(args[:4]))
    return proc


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run("sudo", "docker", *args, check=check)


def inspect(name: str) -> dict:
    return json.loads(docker("inspect", name).stdout)[0]


def image_id(tag: str) -> str | None:
    result = docker("image", "inspect", tag, "--format", "{{.Id}}", check=False)
    return result.stdout.strip() if result.returncode == 0 else None


def config_values(raw: bytes) -> dict[str, str]:
    values = {}
    for line in raw.decode("utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError("Malformed production env file")
        key, value = line.split("=", 1)
        if key in values:
            raise RuntimeError("Duplicate production env setting: " + key)
        values[key] = value
    return values


def update_env(raw: bytes, image: str) -> bytes:
    lines = raw.decode("utf-8").splitlines(keepends=True)
    matches = [i for i, line in enumerate(lines) if line.startswith("TELEGRAM_IMAGE=")]
    if len(matches) > 1:
        raise RuntimeError("Duplicate TELEGRAM_IMAGE")
    if matches:
        lines[matches[0]] = "TELEGRAM_IMAGE=" + image + "\n"
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append("TELEGRAM_IMAGE=" + image + "\n")
    return "".join(lines).encode("utf-8")


def atomic_env(raw: bytes) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=".env.production.", dir=ROOT)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, ENV)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def compose(*args: str) -> None:
    docker("compose", "-p", PROJECT, "--env-file", str(ENV),
           "-f", str(COMPOSE), *args)


def wait_healthy(seconds: int = 90) -> dict:
    for _ in range(seconds // 2):
        result = docker("inspect", NEW, check=False)
        if result.returncode == 0:
            current = json.loads(result.stdout)[0]
            status = current["State"]["Status"]
            health = current["State"].get("Health", {}).get("Status")
            if status == "running" and health == "healthy":
                return current
            if status in ("exited", "dead") or health == "unhealthy":
                raise RuntimeError("New Telegram container is " + str(health or status))
        time.sleep(2)
    raise RuntimeError("New Telegram container did not become healthy in time")


def verify(old: dict, new: dict, state: Path, network: str) -> None:
    labels = new["Config"]["Labels"]
    assert labels["com.docker.compose.project"] == PROJECT
    assert labels["com.docker.compose.service"] == "telegram-delivery"
    assert new["Image"] == old["Image"], "Image changed during rename"
    assert new["Config"]["User"] == old["Config"]["User"] == "10001:10001"
    assert network in new["NetworkSettings"]["Networks"]
    aliases = new["NetworkSettings"]["Networks"][network].get("Aliases") or []
    assert "telegram-delivery" in aliases, "Stable DNS alias is missing"
    mounts = [m for m in new["Mounts"] if m["Destination"] == "/data"]
    assert len(mounts) == 1 and Path(mounts[0]["Source"]).resolve() == state
    assert mounts[0]["RW"], "SQLite mount must remain writable"
    def private_env(container: dict) -> dict[str, str]:
        values = dict(v.split("=", 1) for v in container["Config"]["Env"] if "=" in v)
        return {k: values.get(k) for k in (
            "TELEGRAM_ENCRYPTION_KEY", "TELEGRAM_ADMIN_TOKEN", "TELEGRAM_DB_PATH")}
    assert private_env(new) == private_env(old), "Private runtime settings changed"
    assert new["HostConfig"]["PortBindings"] in (None, {}), "Unexpected public port"
    docker("exec", "switchboard-prod-core", "python", "-c",
           "import urllib.request; "
           "r=urllib.request.urlopen('http://telegram-delivery:8781/health',timeout=5); "
           "assert r.status==200")


def verify_private_paths(state: Path, secret_file: Path) -> None:
    """Check protected production paths as root without reading their contents.

    Telegram's state and credentials may be owned by root or UID 10001.
    A user-level Path.is_file() can raise PermissionError before cutover.
    """
    for kind, path in (
        ("-d", state),
        ("-f", state / "telegram.sqlite3"),
        ("-f", secret_file),
    ):
        result = run("sudo", "test", kind, str(path), check=False)
        if result.returncode:
            raise RuntimeError("Missing or inaccessible private production path: "
                               + str(path))


def main() -> None:
    run("sudo", "-v")
    if not ENV.is_file():
        raise RuntimeError("Missing .env.production")
    original = ENV.read_bytes()
    values = config_values(original)
    for key in ("TELEGRAM_STATE_DIR", "TELEGRAM_ENV_FILE", "TELEGRAM_GATEWAY_NETWORK"):
        if not values.get(key):
            raise RuntimeError("Missing " + key)
    state = Path(values["TELEGRAM_STATE_DIR"]).resolve()
    secret_file = Path(values["TELEGRAM_ENV_FILE"])
    network = values["TELEGRAM_GATEWAY_NETWORK"]
    verify_private_paths(state, secret_file)
    old = inspect(OLD)
    assert old["State"]["Running"], "Existing Telegram receiver is not running"
    assert old["Config"]["Labels"].get("com.docker.compose.project") == "nogai-telegram-standalone"
    assert old["Config"]["Labels"].get("com.docker.compose.service") == "telegram-delivery"
    assert network in old["NetworkSettings"]["Networks"], "Old network mismatch"
    old_mount = [m for m in old["Mounts"] if m["Destination"] == "/data"]
    assert len(old_mount) == 1 and Path(old_mount[0]["Source"]).resolve() == state
    assert docker("inspect", NEW, check=False).returncode != 0, "New container already exists"
    assert image_id(IMAGE) in (None, old["Image"]), "New image tag already points elsewhere"
    docker("tag", old["Image"], IMAGE)
    print("PASS: exact running image retagged; no build", flush=True)

    old_tag = values.get("TELEGRAM_IMAGE", "")
    atomic_env(update_env(original, IMAGE))
    try:
        compose("config", "--quiet")
    except BaseException:
        atomic_env(original)
        raise
    print("PASS: new Compose config validated; existing receiver unchanged", flush=True)

    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = Path.home() / "switchboard-cutover-backups" / ("telegram-rename-" + stamp)
    print("=== Stop the sole Telegram receiver and create private backup ===", flush=True)
    stopped = False
    try:
        docker("stop", OLD)
        stopped = True
        run("sudo", "install", "-d", "-m", "0700", str(backup))
        run("sudo", "tar", "-czf", str(backup / "state.tar.gz"), "-C", str(state), ".")
        run("sudo", "cp", "-a", str(ENV), str(backup / "env.production"))
        run("sudo", "cp", "-a", str(secret_file), str(backup / "telegram.env"))
        print("PASS: offline SQLite directory and runtime settings backed up", flush=True)

        print("=== Start renamed Telegram Compose project ===", flush=True)
        compose("up", "-d", "--no-deps", "--no-build", "telegram-delivery")
        new = wait_healthy()
        verify(old, new, state, network)
        print("PASS: new Telegram healthy, same image/state/secrets and DNS", flush=True)
    except BaseException:
        if stopped:
            print("ERROR: cutover failed; restoring original receiver", file=sys.stderr, flush=True)
            docker("rm", "-f", NEW, check=False)
            atomic_env(original)
            result = docker("start", OLD, check=False)
            if result.returncode:
                print("CRITICAL: automatic restart failed; manually inspect Telegram",
                      file=sys.stderr, flush=True)
            else:
                print("PASS: original Telegram receiver restarted", file=sys.stderr, flush=True)
        else:
            atomic_env(original)
        raise

    # Only retire the old container once the replacement has passed all checks.
    removal = docker("rm", OLD, check=False)
    if removal.returncode:
        print("WARN: old stopped Telegram container remains; remove it manually", flush=True)
    elif old_tag and old_tag != IMAGE and image_id(old_tag) == old["Image"]:
        docker("image", "rm", old_tag, check=False)
    print("PASS: Telegram Compose rename complete", flush=True)
    print("Private rollback backup:", backup, flush=True)
    print("Next: update Logging's project list and restart its source watcher.", flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("FAILED:", error, file=sys.stderr)
        sys.exit(1)
