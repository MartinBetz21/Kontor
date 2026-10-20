import pytest

from pathlib import Path

from kontor.core.allocation import Part, allocate
from kontor.core.errors import KontorError
from kontor.services import documents as docs
from kontor.services import finalize as fin
from kontor.services import masterdata as md
from kontor.services import payments as pay

LOGO = Path(__file__).parent / "fixtures" / "logo-hell.svg"


@pytest.fixture
def invoice(ctx, ui):
    (ctx.data / "logo.svg").write_bytes(LOGO.read_bytes())
    md.set_settings(ctx, {"name": "Erika Mustermann", "street": "Musterstraße 1", "postcode": "10115", "city": "Berlin",
                          "tax_number": "123/456/78901", "logo_path": "logo.svg"})
    cid = md.create_customer(ctx, {"company": "Kunde", "street": "Weg 1", "postcode": "12345", "city": "Ort"})

    def make(amount=100000):
        did = docs.save_draft(ctx, {"kind": "rechnung", "customer_id": cid, "service_date": "2026-09-30",
                                    "lines": [{"title": "Leistung", "quantity": "1", "unit_price": amount}]})
        return fin.finalize(ui, did)["id"]
    return make


def test_allocate_full_and_partial():
    assert allocate(29900, [("19", 29900, 4774)]) == [Part("19", 25126, 4774)]
    first = allocate(50000, [("19", 119000, 19000)])
    assert first == [Part("19", 42017, 7983)]
    assert allocate(69000, [("19", 69000, 11017)]) == [Part("19", 57983, 11017)]


def test_partial_then_rest_sums_exactly(ctx, invoice):
    did = invoice()
    pay.record_payment(ctx, did, "2026-10-10", "500,00")
    assert docs.get_document(ctx, did)["status"] == "teilbezahlt"
    pay.record_payment(ctx, did, "2026-11-03", 69000)
    d = docs.get_document(ctx, did)
    assert (d["status"], d["open"]) == ("bezahlt", 0)
    parts = ctx.conn.execute("SELECT SUM(net), SUM(tax) FROM payment_parts").fetchone()
    assert tuple(parts) == (100000, 19000)


def test_overpayment_and_bad_input_refused(ctx, invoice):
    did = invoice()
    with pytest.raises(KontorError, match="offenen Betrag"):
        pay.record_payment(ctx, did, "2026-10-10", 119001)
    pay.record_payment(ctx, did, "2026-10-10", 119000)
    with pytest.raises(KontorError, match="offenen Betrag"):
        pay.record_payment(ctx, did, "2026-10-11", 119000)    # doppelt erfasst
    with pytest.raises(KontorError):
        pay.record_payment(ctx, did, "10.10.2026", 1)
    with pytest.raises(KontorError):
        pay.record_payment(ctx, did, "2026-10-10", 0)


def test_no_payment_on_draft_or_offer(ctx, invoice):
    did = docs.save_draft(ctx, {"kind": "rechnung", "lines": [{"title": "x", "quantity": "1", "unit_price": 100}]})
    with pytest.raises(KontorError):
        pay.record_payment(ctx, did, "2026-10-10", 100)


def test_refund_on_storno(ctx, ui, invoice):
    did = invoice()
    pay.record_payment(ctx, did, "2026-10-10", 119000)
    storno = fin.cancel(ui, did)
    pay.record_payment(ctx, storno["id"], "2026-10-20", -119000, "Erstattung")
    assert ctx.conn.execute("SELECT SUM(tax) FROM payment_parts").fetchone()[0] == 0
    with pytest.raises(KontorError):
        pay.record_payment(ctx, storno["id"], "2026-10-21", -1)


def test_tax_payment_and_listing(ctx, invoice):
    pay.record_tax_payment(ctx, "2026-11-10", "-190,00", "UStVA 2026-10")
    rows = pay.list_payments(ctx, year=2026)
    assert [(r["kind"], r["amount"]) for r in rows] == [("ust", -19000)]


def test_wrong_payment_can_be_deleted_by_a_human_only(ctx, ui, invoice):
    from kontor.core.errors import HumanOnly
    did = invoice()
    pid = pay.record_payment(ctx, did, "2026-10-10", 119000)
    with pytest.raises(HumanOnly):
        pay.delete_payment(ctx, pid, "Tippfehler")
    with pytest.raises(KontorError, match="Begründung"):
        pay.delete_payment(ui, pid, "")
    pay.delete_payment(ui, pid, "Tippfehler: 119,00 statt 1.190,00")
    d = docs.get_document(ctx, did)
    assert (d["status"], d["open"]) == ("abgeschlossen", 119000)
    assert ctx.conn.execute("SELECT COUNT(*) FROM payment_parts").fetchone()[0] == 0
