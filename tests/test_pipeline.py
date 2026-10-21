"""Angebots-Pipeline: Zahlungsmodus, Ablauf der Gültigkeit mit Nachfass-Aufgabe, Kunde aus dem Angebot."""
from datetime import date

import pytest

from kontor.core.errors import KontorError
from kontor.reports.overview import overview
from kontor.services import documents as docs
from kontor.services import finalize as fin
from kontor.services import masterdata as md
from kontor.services import pipeline
from tests.test_finalize import setup  # noqa: F401


def offer(ui, customer, valid_until="2026-10-31", mode="50_50", net=800000):
    did = docs.save_draft(ui, {"kind": "angebot", "customer_id": customer, "valid_until": valid_until,
                               "payment_mode": mode, "lines": [{"title": "Entwicklung", "quantity": "1",
                                                                "unit_price": net}]})
    return fin.finalize(ui, did)


def test_payment_mode_stored_and_locked(ui, setup):
    o = offer(ui, setup)
    assert o["payment_mode"] == "50_50"
    with pytest.raises(KontorError):
        docs.save_draft(ui, {"kind": "angebot", "payment_mode": "irgendwann", "lines": []})


@pytest.mark.parametrize("mode,kinds", [("vorab", ["rechnung"]), ("danach", ["rechnung"]),
                                        ("50_50", ["abschlag"])])
def test_invoice_follows_payment_mode(ui, setup, mode, kinds):
    o = offer(ui, setup, mode=mode)
    docs.set_status(ui, o["id"], "angenommen")
    new = pipeline.first_invoice(ui, o["id"])
    d = docs.get_document(ui, new)
    assert d["kind"] == kinds[0]
    if mode == "50_50":
        assert d["net"] == 400000


def test_expired_offer_is_rejected_with_followup(ui, setup):
    o = offer(ui, setup, valid_until="2026-10-01")          # ui.today = 2026-10-02
    assert pipeline.expire_offers(ui) == [o["id"]]
    assert docs.get_document(ui, o["id"])["status"] == "abgelehnt"
    tasks = pipeline.open_followups(ui)
    assert [(t["document_id"], t["reason"]) for t in tasks] == [(o["id"], "abgelaufen")]
    assert pipeline.expire_offers(ui) == []                  # nur einmal
    assert overview(ui)["followups"][0]["number"] == o["number"]
    docs.set_status(ui, o["id"], "angenommen")               # gerettet
    pipeline.close_followup(ui, tasks[0]["id"], "Max hat doch zugesagt")
    assert pipeline.open_followups(ui) == []


def test_pipeline_groups(ui, setup):
    a = offer(ui, setup)
    b = offer(ui, setup)
    docs.set_status(ui, b["id"], "angenommen")
    p = pipeline.board(ui)
    assert [x["id"] for x in p["verschickt"]] == [a["id"]] and [x["id"] for x in p["angenommen"]] == [b["id"]]
    assert p["sums"]["verschickt"] == 800000


def test_new_customer_from_offer(ui):
    did = docs.save_draft(ui, {"kind": "angebot", "new_customer": {"company": "Neu GmbH", "contact": "Anna Muster",
                                                                   "street": "Weg 1", "postcode": "50667", "city": "Köln"},
                               "lines": []})
    d = docs.get_document(ui, did)
    assert d["customer"]["company"] == "Neu GmbH"
    assert [c["company"] for c in md.list_customers(ui)] == ["Neu GmbH"]
