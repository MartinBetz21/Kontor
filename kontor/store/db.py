"""SQLite im Autocommit-Modus; Transaktionen nur über tx(). Journal-Modus DELETE, damit die Datei
nach jeder Transaktion für sich konsistent ist (sie wird im lokalen git versioniert)."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from ..config import CODE_ROOT
from ..core.errors import KontorError

MIGRATIONS = CODE_ROOT / "migrations"


def connect(path: Path, migrate_schema: bool = True) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    if path.exists() and path.stat().st_mode & 0o077:
        os.chmod(path, 0o600)                   # Kunden, Beträge, Bankdaten: nur der eigene Benutzer liest mit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = DELETE")
    if migrate_schema:
        migrate(conn)
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    current = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0] or 0
    for path in sorted(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        number = int(path.name[:4])
        if number <= current:
            continue
        try:
            conn.executescript(
                "BEGIN;\n" + path.read_text(encoding="utf-8")
                + f"\nINSERT INTO schema_version VALUES ({number});\nCOMMIT;"
            )
        except Exception:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        current = number
    return current


@contextmanager
def tx(conn: sqlite3.Connection):
    """Eine Transaktion. Läuft schon eine, wird mitgemacht (die äußere entscheidet über Commit)."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except sqlite3.IntegrityError as exc:
        conn.execute("ROLLBACK")
        msg = str(exc)
        if msg.startswith("ledger: "):          # Präfix der Trigger aus den Migrationen (Name vor 05.10.2026)
            raise KontorError(msg[len("ledger: "):]) from exc
        raise
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
