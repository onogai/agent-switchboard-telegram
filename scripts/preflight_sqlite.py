#!/usr/bin/env python3
"""Audit an existing Telegram SQLite via a disposable online-backup copy.

Does not mutate the input DB and does not print encrypted credentials or chat IDs.
Run from the Telegram repository root with TELEGRAM_ENCRYPTION_KEY set privately.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from tempfile import TemporaryDirectory

from cryptography.fernet import Fernet, InvalidToken

# Invoking "python3 scripts/preflight_sqlite.py" puts scripts/ rather than the
# repository root on sys.path. Resolve the local package from this file, not
# the caller's working directory or an externally supplied PYTHONPATH.
REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from telegram_service.store import Store

# Existing columns are retained byte-for-byte except these explicit migrations:
# historical idempotency_key becomes binding-scoped; new columns are added.
EXCLUDED_LEGACY_COLUMNS = {
    "deliveries": {"idempotency_key", "binding_id", "bot_id", "chat_id",
                   "logical_idempotency_key"},
    "destinations": {"binding_id"},
}


def tables(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def fingerprint(db: sqlite3.Connection, table: str, columns: list[str]) -> str:
    if not columns:
        return ""
    quoted = ", ".join('"' + col.replace('"', '""') + '"' for col in columns)
    rows = [json.dumps(tuple(row), ensure_ascii=False, separators=(",", ":"), default=str)
            for row in db.execute(f'SELECT {quoted} FROM "{table}"')]
    payload = "\n".join(sorted(rows))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def verify_integrity(db: sqlite3.Connection) -> None:
    if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise RuntimeError("SQLite integrity check failed")


def audit(source: Path, encryption_key: str) -> dict[str, int]:
    source = source.expanduser().absolute()
    try:
        if not source.is_file() or source.is_symlink():
            raise RuntimeError("Input must be an existing regular SQLite database, not a symlink")
        if not os.access(source, os.R_OK):
            raise RuntimeError("Source SQLite is not readable by this user; check file and parent directory permissions")
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(source) + suffix)
            if sidecar.exists() and not os.access(sidecar, os.R_OK):
                raise RuntimeError("Existing SQLite WAL/SHM sidecar is not readable by this user")
    except PermissionError:
        raise RuntimeError("Source SQLite or its parent directory cannot be accessed by this user") from None
    if not encryption_key:
        raise RuntimeError("TELEGRAM_ENCRYPTION_KEY is required")
    cipher = Fernet(encryption_key.encode("ascii"))

    with TemporaryDirectory(prefix="telegram-preflight-") as directory:
        copy = Path(directory) / "audit.sqlite3"
        # Mode=ro + SQLite online backup reads a consistent WAL snapshot without
        # holding a production writer transaction or changing source contents.
        uri = source.as_uri() + "?mode=ro"
        with sqlite3.connect(uri, uri=True, timeout=15) as original:
            with sqlite3.connect(copy) as destination:
                original.backup(destination)
        os.chmod(copy, 0o600)

        with sqlite3.connect(copy) as baseline:
            verify_integrity(baseline)
            original_tables = tables(baseline)
            if not {"bots", "destinations", "deliveries", "summaries"} <= original_tables:
                raise RuntimeError("Input does not have the expected Telegram tables")
            counts = {}
            columns = {}
            checksums = {}
            for table in sorted(original_tables):
                if table.startswith("sqlite_"):
                    continue
                counts[table] = baseline.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                columns[table] = [row[1] for row in baseline.execute(
                    f'PRAGMA table_info("{table}")')]
                legacy_fields = [col for col in columns[table]
                                 if col not in EXCLUDED_LEGACY_COLUMNS.get(table, set())]
                checksums[table] = fingerprint(baseline, table, legacy_fields)
            # Validate *all* saved bot tokens, including disabled bots; no network.
            for (encrypted,) in baseline.execute("SELECT encrypted_token FROM bots"):
                try:
                    cipher.decrypt(encrypted.encode("ascii"))
                except (InvalidToken, ValueError, AttributeError):
                    raise RuntimeError("Stored Telegram bot credential cannot be decrypted") from None

        # Store.initialize applies the current add-only migrations to COPY only.
        Store(copy, encryption_key)
        with sqlite3.connect(copy) as migrated:
            verify_integrity(migrated)
            new_tables = tables(migrated)
            for table in original_tables:
                if table not in new_tables:
                    raise RuntimeError("Migration dropped a source table")
                count = migrated.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                if count != counts[table]:
                    raise RuntimeError("Migration changed existing row counts")
                legacy_fields = [col for col in columns[table]
                                 if col not in EXCLUDED_LEGACY_COLUMNS.get(table, set())]
                if fingerprint(migrated, table, legacy_fields) != checksums[table]:
                    raise RuntimeError("Migration modified protected legacy fields")
            # Report ONLY row counts, never bot tokens, physical chat IDs or keys.
            return {table: counts[table] for table in sorted(counts)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path,
                        help="Existing Telegram SQLite file (read-only source)")
    args = parser.parse_args()
    try:
        result = audit(args.db, os.environ.get("TELEGRAM_ENCRYPTION_KEY", ""))
    except Exception as exc:
        # Do not expose raw sqlite exceptions, which can include host paths.
        print("FAIL Telegram preflight: " + (
            str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__), file=sys.stderr)
        return 1
    print("PASS Telegram SQLite snapshot/migration/integrity/token decryption")
    for table, count in result.items():
        print(f"{table}: {count} records")
    print("Production SQLite unchanged; disposable snapshot removed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
