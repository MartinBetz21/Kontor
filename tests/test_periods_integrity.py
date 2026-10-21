import os

import pytest

from kontor.core.errors import HumanOnly, KontorError
from kontor.services import integrity, periods
from kontor.services import payments as pay
from tests.test_payments import invoice  # noqa: F401  (Fixture)


def test_lock_blocks_payments_and_unlock_needs_reason(ctx, ui, invoice):
    did = invoice()
    with pytest.raises(HumanOnly):
        periods.lock_month(ctx, "2026-10")
    periods.lock_month(ui, "2026-10", "UStVA abgegeben")
    with pytest.raises(KontorError, match="abgeschlossen"):
        pay.record_payment(ctx, did, "2026-10-15", 1000)
    with pytest.raises(KontorError, match="Begründung"):
        periods.unlock_month(ui, "2026-10", "")
    periods.unlock_month(ui, "2026-10", "Zahlung vergessen")
    pay.record_payment(ctx, did, "2026-10-15", 1000)
    assert periods.locked_months(ui) == []
    with pytest.raises(KontorError):
        periods.lock_month(ui, "Oktober")


def test_integrity_clean_then_detects_tampered_archive(ctx, invoice):
    did = invoice()
    assert integrity.check(ctx) == []
    path = next((ctx.data / "archiv").rglob("*.pdf"))
    os.chmod(path, 0o644)
    path.write_bytes(path.read_bytes() + b"%manipuliert")
    assert any("Prüfsumme" in p for p in integrity.check(ctx))


def test_integrity_detects_number_gap(ctx, invoice):
    invoice()
    ctx.conn.execute("UPDATE number_counters SET last = 3")
    problems = integrity.check(ctx)
    assert any("RE" in p and "002 fehlt" in p for p in problems) and any("003 fehlt" in p for p in problems)


def test_integrity_detects_row_differing_from_snapshot(ctx, invoice):
    did = invoice()
    ctx.conn.execute("DROP TRIGGER documents_locked_update")       # Abweichung am System vorbei simulieren
    ctx.conn.execute("UPDATE documents SET gross = gross + 1 WHERE id = ?", (did,))
    assert any("Schnappschuss" in p and "Summe" in p for p in integrity.check(ctx))
