import pytest

from kontor.core.errors import KontorError
from kontor.core.status import check_transition, payment_status
from kontor.services import documents as docs
from kontor.services import masterdata as md

OFFER_LINES = [
    {"title": "Entwicklung des Bestellportals", "quantity": "1", "unit": "psch.", "unit_price": 400000},
    {"title": "Anbindung an bestehende Systeme", "quantity": "1", "unit": "psch.", "unit_price": "2.500,00"},
    {"title": "Betreuung in den ersten drei Monaten", "quantity": "3", "unit": "Monate", "unit_price": 60000},
]


@pytest.fixture
def customer(ctx):
    return md.create_customer(ctx, {"company": "Beispiel Technik GmbH & Co. KG", "contact": "Max Beispiel",
                                    "street": "Musterweg 1", "postcode": "10115", "city": "Berlin"})


def offer(ctx, customer):
    return docs.save_draft(ctx, {"kind": "angebot", "customer_id": customer, "subject": "Bestellportal",
                                 "lines": OFFER_LINES,
                                 "allowances": [{"label": "Gutschrift Vorgespräch", "amount": -80000}]})


def test_offer_draft_totals_and_defaults(ctx, customer):
    md.create_template(ctx, {"kind": "angebot", "slot": "schluss", "title": "S",
                             "body": "Zur Annahme genügt eine kurze Antwort per Mail.", "is_default": True})
    d = docs.get_document(ctx, offer(ctx, customer))
    assert (d["status"], d["number"], d["net"], d["tax"], d["gross"]) == ("entwurf", None, 750000, 142500, 892500)
    assert d["valid_until"] == "2026-11-01"           # 02.10. + 30 Tage
    assert d["closing"] == "Zur Annahme genügt eine kurze Antwort per Mail."
    assert [l["total"] for l in d["lines"]] == [400000, 250000, 180000]
    assert d["customer"]["company"] == "Beispiel Technik GmbH & Co. KG"


def test_invoice_draft_due_date_and_edit(ctx, customer):
    did = docs.save_draft(ctx, {"kind": "rechnung", "customer_id": customer, "service_date": "2026-09-30",
                                "lines": [{"title": "Prozessanalyse", "quantity": "1", "unit_price": 100000}]})
    assert docs.get_document(ctx, did)["due_date"] == "2026-10-16"
    docs.save_draft(ctx, {"subject": "Prozessanalyse Auftragsabwicklung"}, did)
    d = docs.get_document(ctx, did)
    assert d["subject"] == "Prozessanalyse Auftragsabwicklung" and d["gross"] == 119000
    assert len(d["lines"]) == 1                        # Positionen bleiben, wenn nicht mitgeschickt


def test_draft_validation(ctx, customer):
    with pytest.raises(KontorError):
        docs.save_draft(ctx, {"kind": "gutschrift", "lines": []})
    with pytest.raises(KontorError):
        docs.save_draft(ctx, {"kind": "rechnung", "doc_date": "02.10.2026", "lines": []})
    with pytest.raises(KontorError, match="Bezeichnung"):
        docs.save_draft(ctx, {"kind": "rechnung", "lines": [{"title": "", "quantity": "1", "unit_price": 1}]})
    with pytest.raises(KontorError):
        docs.save_draft(ctx, {"kind": "rechnung", "lines": [{"title": "x", "quantity": "1", "unit_price": "abc"}]})


def test_delete_draft_and_list(ctx, customer):
    a = offer(ctx, customer)
    r = docs.save_draft(ctx, {"kind": "rechnung", "customer_id": customer, "lines": []})
    assert [d["id"] for d in docs.list_documents(ctx, group="angebote")] == [a]
    assert [d["id"] for d in docs.list_documents(ctx, group="rechnungen")] == [r]
    docs.delete_draft(ctx, r)
    assert docs.list_documents(ctx, group="rechnungen") == []


def test_status_rules():
    check_transition("angebot", "abgeschlossen", "angenommen")
    with pytest.raises(KontorError):
        check_transition("rechnung", "entwurf", "bezahlt")
    with pytest.raises(KontorError):
        check_transition("storno", "abgeschlossen", "storniert")
    assert payment_status(119000, 0) == "abgeschlossen"
    assert payment_status(119000, 50000) == "teilbezahlt"
    assert payment_status(119000, 119000) == "bezahlt"


def test_status_change_on_draft_is_refused(ctx, customer):
    with pytest.raises(KontorError):
        docs.set_status(ctx, offer(ctx, customer), "angenommen")


def test_save_line_as_catalog_and_copy(ctx, customer):
    a = offer(ctx, customer)
    iid = docs.save_line_as_catalog(ctx, a, 3)
    assert md.get_catalog_item(ctx, iid)["unit_price"] == 60000
    b = docs.copy_document(ctx, a)
    assert docs.get_document(ctx, b)["gross"] == 892500 and b != a


def test_browser_line_endings_are_normalized(ctx, customer):
    did = docs.save_draft(ctx, {"kind": "angebot", "customer_id": customer, "intro": "Hallo {vorname},\r\n\r\nAbsatz zwei.",
                                "closing": "Gruß\r\nErika", "notes": "a\r\nb", "lines": []})
    d = docs.get_document(ctx, did)
    assert (d["intro"], d["closing"], d["notes"]) == ("Hallo {vorname},\n\nAbsatz zwei.", "Gruß\nErika", "a\nb")
