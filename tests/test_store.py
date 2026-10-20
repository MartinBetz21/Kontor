import sqlite3

import pytest

from kontor.context import require_human
from kontor.core.errors import HumanOnly, KontorError
from kontor.store import audit
from kontor.store.db import MIGRATIONS, connect, migrate, tx


def test_fresh_db_is_migrated(ctx):
    v = ctx.conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
    assert v == len(list(MIGRATIONS.glob("[0-9][0-9][0-9][0-9]_*.sql")))
    assert ctx.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert ctx.conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_migrate_twice_is_noop(ctx):
    before = ctx.conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
    migrate(ctx.conn)
    assert ctx.conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == before


def test_reopen_keeps_data(ctx):
    ctx.conn.execute("INSERT INTO settings VALUES ('name','Erika Mustermann')")
    path = ctx.data / "kontor.db"
    conn = connect(path)
    assert conn.execute("SELECT value FROM settings WHERE key='name'").fetchone()[0] == "Erika Mustermann"


def test_db_and_data_folder_are_private(ctx):
    """Andere Benutzer am Rechner lesen weder Datenbank noch Belege und PDFs."""
    assert (ctx.data / "kontor.db").stat().st_mode & 0o777 == 0o600
    assert ctx.data.stat().st_mode & 0o777 == 0o700


def test_existing_open_db_gets_tightened(ctx):
    import os
    from kontor.context import open_ctx
    os.chmod(ctx.data / "kontor.db", 0o644)
    os.chmod(ctx.data, 0o755)
    open_ctx("cli", ctx.data).conn.close()
    assert (ctx.data / "kontor.db").stat().st_mode & 0o777 == 0o600
    assert ctx.data.stat().st_mode & 0o777 == 0o700


def test_tx_rolls_back(ctx):
    with pytest.raises(RuntimeError):
        with tx(ctx.conn):
            ctx.conn.execute("INSERT INTO settings VALUES ('a','b')")
            raise RuntimeError("boom")
    assert ctx.conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0


def test_nested_tx_joins_outer(ctx):
    with tx(ctx.conn):
        with tx(ctx.conn):
            ctx.conn.execute("INSERT INTO settings VALUES ('a','b')")
        assert ctx.conn.in_transaction
    assert ctx.conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 1


def test_trigger_error_becomes_kontor_error(ctx):
    ctx.conn.execute("INSERT INTO documents (kind, status, doc_date) VALUES ('rechnung','abgeschlossen','2026-10-02')")
    with pytest.raises(KontorError, match="unveränderbar"):
        with tx(ctx.conn):
            ctx.conn.execute("UPDATE documents SET subject='x'")


def test_finalized_document_status_may_change_but_not_back(ctx):
    ctx.conn.execute("INSERT INTO documents (kind, status, doc_date) VALUES ('rechnung','abgeschlossen','2026-10-02')")
    ctx.conn.execute("UPDATE documents SET status='bezahlt'")
    with pytest.raises(sqlite3.IntegrityError):
        ctx.conn.execute("UPDATE documents SET status='entwurf'")
    with pytest.raises(sqlite3.IntegrityError):
        ctx.conn.execute("DELETE FROM documents")


def test_locked_month_blocks_payments(ctx):
    ctx.conn.execute("INSERT INTO period_locks VALUES ('2026-09','2026-10-10T10:00:00','')")
    with pytest.raises(sqlite3.IntegrityError, match="abgeschlossen"):
        ctx.conn.execute("INSERT INTO payments (pay_date, amount, kind) VALUES ('2026-09-30', 100, 'ust')")
    ctx.conn.execute("INSERT INTO payments (pay_date, amount, kind) VALUES ('2026-10-01', 100, 'ust')")


def test_audit_chain_verifies_and_detects_tampering(ctx):
    audit.log(ctx.conn, "cli", "anlegen", "kunde", 1, {"firma": "Beispiel Technik"})
    audit.log(ctx.conn, "ui", "abschliessen", "dokument", 7, {"nummer": "RE-2026-001"})
    assert audit.verify(ctx.conn) == []
    with pytest.raises(sqlite3.IntegrityError):
        ctx.conn.execute("UPDATE audit_log SET payload='{}' WHERE id=1")
    ctx.conn.execute("DROP TRIGGER audit_no_update")          # Manipulation am System vorbei simulieren
    ctx.conn.execute("UPDATE audit_log SET payload='{}' WHERE id=1")
    assert any("#1" in p for p in audit.verify(ctx.conn))


def test_require_human(ctx, ui):
    with pytest.raises(HumanOnly):
        require_human(ctx)
    require_human(ui)


# Sobald 0002 existiert: Test „Stand N−1 mit Daten → aktueller Stand ohne Datenverlust“ ergänzen (Spec 13).


def test_default_categories_exist(ctx):
    assert ctx.conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0] >= 10
