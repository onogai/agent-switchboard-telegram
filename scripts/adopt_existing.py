#!/usr/bin/env python3
"""Guarded migration of a stopped legacy Telegram service into staged schema-v2 state.

Run on the Docker host. No live sends, no Docker container starts/stops, no
Registry or Manager mutations; secrets are never accepted on the command line.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.preflight_sqlite import audit

LEGACY_CONTAINER = "switchboard-telegram-delivery"
PROJECT = "switchboard-addon-telegramhub"


def _file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{label} must be a regular, non-symlink file")
    return path


def _env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in _file(path, "Legacy environment").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise RuntimeError("Malformed legacy environment file")
        name, value = line.split("=", 1)
        if name.startswith("export "):
            name = name[len("export "):]
        value = value.strip()
        if value.startswith(("'", '"')) and value.endswith(value[:1]):
            value = value[1:-1]
        values[name.strip()] = value
    return values


def _docker(*argv: str) -> str:
    try:
        p = subprocess.run(["docker", *argv], text=True, capture_output=True,
                           timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise RuntimeError("Unable to inspect Docker state") from None
    if p.returncode:
        raise RuntimeError("Docker inspection failed")
    return p.stdout.strip()


def _legacy_status_and_identity(secrets: dict[str, str]) -> bool:
    raw = json.loads(_docker("inspect", LEGACY_CONTAINER))
    if not isinstance(raw, list) or len(raw) != 1:
        raise RuntimeError("Unexpected legacy Docker container")
    old = raw[0]
    runtime = dict(row.split("=", 1) for row in old["Config"]["Env"] if "=" in row)
    for name in ("TELEGRAM_ENCRYPTION_KEY", "TELEGRAM_ADMIN_TOKEN"):
        if not secrets.get(name) or runtime.get(name) != secrets[name]:
            raise RuntimeError("Legacy credentials differ from the old container")
    if old["Config"].get("Labels", {}).get("com.docker.compose.project") != "switchboard-telegram":
        raise RuntimeError("Unexpected legacy Compose ownership")
    return bool(old["State"]["Running"])


def _candidate_running() -> bool:
    return bool(_docker("ps", "--filter", f"label=com.docker.compose.project={PROJECT}",
                        "--format", "{{.ID}}"))


def _candidate(instance: Path) -> tuple[Path, Path]:
    if instance.is_symlink() or not instance.is_dir():
        raise RuntimeError("Instance root must be an existing directory, not symlink")
    metadata = json.loads(_file(instance / "installation.json", "Installation metadata")
                          .read_text(encoding="utf-8"))
    if not (metadata.get("instance") == "telegramhub"
            and metadata.get("source_id") == "telegramhub"
            and metadata.get("addon_id") == "telegramhub"
            and metadata.get("schema") == "2"
            and metadata.get("target") == "production"
            and isinstance(metadata.get("staged_replacement"), dict)):
        raise RuntimeError("Expected staged production telegramhub instance")
    if not (instance / "source" / "switchboard-addon.yaml").is_file():
        raise RuntimeError("Installed Telegram add-on source is missing")
    state = instance / "state"
    database = state / "database" / "telegram.sqlite3"
    if (state.is_symlink() or (state / "database").is_symlink()
            or not (state / "database").is_dir()):
        raise RuntimeError("Unexpected managed state layout")
    for name in ("encryptionkey", "admintoken"):
        _file(state / "secrets" / name, "Generated secret file")
    if (state / ".telegram-adopted").exists():
        raise RuntimeError("Legacy Telegram state was already imported")
    if database.exists():
        _file(database, "Candidate database")
        with sqlite3.connect(database) as db:
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Candidate database is not healthy")
            names = {row[0] for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for name in names - {"sqlite_sequence"}:
                if db.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]:
                    raise RuntimeError("Candidate database is not empty; refusing replacement")
    if any(Path(str(database) + suffix).exists() for suffix in ("-wal", "-shm")):
        raise RuntimeError("Candidate SQLite has WAL/SHM sidecars; inspect before replacing")
    return state, database


def _write_secret(path: Path, value: str) -> None:
    candidate = path.with_name(path.name + ".new")
    fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as out:
            out.write(value + "\n")
        os.replace(candidate, path)
    finally:
        candidate.unlink(missing_ok=True)


def migrate(instance: Path, db_path: Path, legacy_env: Path, apply: bool) -> dict:
    instance = instance.resolve()
    state, target = _candidate(instance)
    secrets = _env(legacy_env)
    _file(db_path, "Legacy database")
    running = _legacy_status_and_identity(secrets)
    if _candidate_running():
        if apply:
            raise RuntimeError("Stop the candidate add-on before importing state")
    if running and apply:
        raise RuntimeError("Stop the legacy Telegram container before importing state")
    # Read-only integrity, schema migration and stored bot-token decryption test.
    counts = audit(db_path, secrets["TELEGRAM_ENCRYPTION_KEY"])
    if len(secrets["TELEGRAM_ADMIN_TOKEN"]) < 32:
        raise RuntimeError("Legacy admin token is too short")
    if not apply:
        return counts

    backup_dir = state / "legacy-backup"
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup = backup_dir / ("legacy-" + stamp + ".sqlite3")
    if backup.exists():
        raise RuntimeError("Backup name collision")
    with TemporaryDirectory(prefix=".telegram-import-", dir=state / "database") as tmp:
        snapshot = Path(tmp) / "snapshot.sqlite3"
        with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as source:
            with sqlite3.connect(snapshot) as dest:
                source.backup(dest)
        os.chmod(snapshot, 0o600)
        # Audit the exact snapshot that will be installed, not just the source.
        audit(snapshot, secrets["TELEGRAM_ENCRYPTION_KEY"])
        with open(snapshot, "rb") as src, open(backup, "xb") as out:
            os.chmod(backup, 0o600)
            while block := src.read(1024 * 1024):
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        _write_secret(state / "secrets" / "encryptionkey",
                      secrets["TELEGRAM_ENCRYPTION_KEY"])
        _write_secret(state / "secrets" / "admintoken",
                      secrets["TELEGRAM_ADMIN_TOKEN"])
        os.replace(snapshot, target)
    marker = state / ".telegram-adopted"
    with open(marker, "x", encoding="ascii") as done:
        done.write("Imported offline legacy Telegram SQLite; never share state with old service.\n")
    os.chmod(marker, 0o600)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", required=True, type=Path)
    parser.add_argument("--legacy-db", required=True, type=Path)
    parser.add_argument("--legacy-env", required=True, type=Path)
    parser.add_argument("--apply", action="store_true",
                        help="Import only after both old and staged containers are stopped")
    args = parser.parse_args()
    try:
        counts = migrate(args.instance, args.legacy_db, args.legacy_env, args.apply)
    except Exception as exc:
        # Never log credential values, Docker environment or raw remote errors.
        error = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        print("FAIL Telegram migration: " + error, file=sys.stderr)
        return 1
    print("PASS " + ("offline imported" if args.apply else "read-only preflight"))
    print("Records: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    if args.apply:
        print("Staged state + encrypted credentials restored; old state untouched.")
        print("Do not run the legacy and managed discovery receivers together.")
    else:
        print("No state, credentials, containers or Registry changed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
