import json
from datetime import date, timedelta
import os
import stat

import pytest

from pathlib import Path

from kontor.core.errors import HumanOnly, KontorError
from kontor.render.zugferd import extract_xml, read_xml
from kontor.services import documents as docs
from kontor.services import finalize as fin
from kontor.services import masterdata as md

LOGO = Path(__file__).parent / "fixtures" / "logo-hell.svg"


@pytest.fixture
def setup(ctx):
    (ctx.data / "logo-hell.svg").write_bytes(LOGO.read_bytes())
    md.set_settings(ctx, {"name": "Erika Mustermann", "trade_name": "Example Consulting", "street": "Musterstraße 1",
                          "postcode": "10115", "city": "Berlin", "email": "info@example.com",
                          "tax_number": "123/456/78901", "iban": "DE89370400440532013000", "logo_path": "logo-hell.svg"})
    return md.create_customer(ctx, {"company": "Beispiel Technik GmbH & Co. KG", "contact": "Max Beispiel",
                                    "street": "Musterweg 1", "postcode": "10115", "city": "Berlin"})


def invoice(ctx, customer, amount=100000, doc_date="2026-10-02"):
    return docs.save_draft(ctx, {"kind": "rechnung", "customer_id": customer, "doc_date": doc_date,
                                 "service_date": "2026-09-30",
                                 "lines": [{"title": "Prozessanalyse", "quantity": "1", "unit_price": amount}]})


def test_only_human_finalizes(ctx, ui, setup):
    did = invoice(ctx, setup)
    with pytest.raises(HumanOnly):
        fin.finalize(ctx, did)
    d = fin.finalize(ui, did)
    assert (d["status"], d["number"]) == ("abgeschlossen", "RE-2026-001")


def test_archive_file_is_readonly_with_matching_hash_and_xml(ui, setup):
    d = fin.finalize(ui, invoice(ui, setup))
    path = ui.data / d["pdf_path"]
    assert path == ui.data / "archiv" / "2026" / "RE-2026-001.pdf"
    assert not os.stat(path).st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    import hashlib
    assert hashlib.sha256(path.read_bytes()).hexdigest() == d["sha256"]
    assert read_xml(extract_xml(path.read_bytes()))["gross"] == 119000
    assert json.loads((path.with_suffix(".json")).read_text())["number"] == "RE-2026-001"


def test_numbers_are_gapless_even_with_deleted_drafts(ui, setup):
    a = invoice(ui, setup)
    b = invoice(ui, setup)
    docs.delete_draft(ui, a)
    c = invoice(ui, setup)
    assert fin.finalize(ui, b)["number"] == "RE-2026-001"
    assert fin.finalize(ui, c)["number"] == "RE-2026-002"


def test_year_change_continues_numbering(ui, setup):
    fin.finalize(ui, invoice(ui, setup))
    assert fin.finalize(ui, invoice(ui, setup, doc_date="2027-01-04"))["number"] == "RE-2027-002"


def test_offers_have_own_sequence_and_need_no_tax_number(ui, setup):
    md.set_settings(ui, {"tax_number": ""})
    oid = docs.save_draft(ui, {"kind": "angebot", "customer_id": setup,
                               "lines": [{"title": "Entwicklung", "quantity": "1", "unit_price": 500000}]})
    assert fin.finalize(ui, oid)["number"] == "AN-2026-001"


def test_invoice_without_tax_number_is_refused(ui, setup):
    md.set_settings(ui, {"tax_number": ""})
    did = invoice(ui, setup)
    with pytest.raises(KontorError, match="Steuernummer"):
        fin.finalize(ui, did)
    assert docs.get_document(ui, did)["status"] == "entwurf"


def test_missing_customer_address_and_service_date(ui, setup):
    cid = md.create_customer(ui, {"company": "Ohne Adresse"})
    did = docs.save_draft(ui, {"kind": "rechnung", "customer_id": cid,
                               "lines": [{"title": "X", "quantity": "1", "unit_price": 100}]})
    with pytest.raises(KontorError) as e:
        fin.finalize(ui, did)
    assert "Straße" in str(e.value) and "Leistungsdatum" in str(e.value)


def test_failed_render_consumes_no_number(ui, setup):
    md.set_settings(ui, {"logo_path": "fehlt.svg"})
    did = invoice(ui, setup)
    with pytest.raises(KontorError, match="Logo"):
        fin.finalize(ui, did)
    assert docs.get_document(ui, did)["status"] == "entwurf"
    assert not list((ui.data / "archiv").rglob("*.pdf"))
    md.set_settings(ui, {"logo_path": "logo-hell.svg"})
    assert fin.finalize(ui, did)["number"] == "RE-2026-001"


def test_finalized_document_cannot_be_edited(ui, setup):
    d = fin.finalize(ui, invoice(ui, setup))
    with pytest.raises(KontorError):
        docs.save_draft(ui, {"subject": "geändert"}, d["id"])
    with pytest.raises(KontorError):
        docs.delete_draft(ui, d["id"])


def test_cancel_creates_storno_with_next_number(ctx, ui, setup):
    d = fin.finalize(ui, invoice(ui, setup))
    with pytest.raises(HumanOnly):
        fin.cancel(ctx, d["id"])
    s = fin.cancel(ui, d["id"])
    assert (s["kind"], s["number"], s["gross"], s["status"]) == ("storno", "RE-2026-002", -119000, "abgeschlossen")
    assert docs.get_document(ui, d["id"])["status"] == "storniert"
    assert b"381" in extract_xml((ui.data / s["pdf_path"]).read_bytes())


def test_schluss_deducts_finalized_abschlag(ui, setup):
    oid = docs.save_draft(ui, {"kind": "angebot", "customer_id": setup,
                               "lines": [{"title": "Entwicklung", "quantity": "1", "unit_price": 800000}]})
    fin.finalize(ui, oid)
    ab = docs.invoice_from_offer(ui, oid, "abschlag", "50")
    docs.save_draft(ui, {"service_date": "2026-10-02"}, ab)
    a = fin.finalize(ui, ab)
    assert a["gross"] == 476000
    sid = docs.invoice_from_offer(ui, oid, "schluss")
    docs.save_draft(ui, {"service_date": "2026-11-15"}, sid)
    s = fin.finalize(ui, sid)
    assert (s["gross"], s["prepaid_gross"], s["prepaid_tax"], s["due"]) == (952000, 476000, 76000, 476000)
    assert json.loads(s["snapshot_json"])["prepaid"][0]["number"] == a["number"]


def test_preview_pdf_for_draft(ui, setup):
    pdf = fin.preview_pdf(ui, invoice(ui, setup))
    assert pdf[:4] == b"%PDF" and extract_xml(pdf) is None


def _schluss_setup(ui, customer):
    oid = docs.save_draft(ui, {"kind": "angebot", "customer_id": customer,
                               "lines": [{"title": "Entwicklung", "quantity": "1", "unit_price": 800000}]})
    fin.finalize(ui, oid)
    ab = docs.invoice_from_offer(ui, oid, "abschlag", "50")
    docs.save_draft(ui, {"service_date": "2026-10-02"}, ab)
    a = fin.finalize(ui, ab)
    sid = docs.invoice_from_offer(ui, oid, "schluss")
    docs.save_draft(ui, {"service_date": "2026-11-15"}, sid)
    return a, fin.finalize(ui, sid)


def test_storno_of_schluss_reverses_only_what_was_billed(ui, setup):
    from kontor.services import payments as pay
    _, s = _schluss_setup(ui, setup)
    st = fin.cancel(ui, s["id"])
    assert (st["gross"], st["prepaid_gross"], st["due"]) == (-952000, -476000, -476000)
    xml = extract_xml((ui.data / st["pdf_path"]).read_bytes())
    assert b"<ram:TotalPrepaidAmount>4760.00</ram:TotalPrepaidAmount>" in xml
    assert b"<ram:DuePayableAmount>4760.00</ram:DuePayableAmount>" in xml
    with pytest.raises(KontorError):
        pay.record_payment(ui, st["id"], "2026-11-20", -952000)
    pay.record_payment(ui, st["id"], "2026-11-20", -476000)


def test_storno_cannot_be_drafted_directly(ctx, setup):
    with pytest.raises(KontorError, match="Storno"):
        docs.save_draft(ctx, {"kind": "storno", "customer_id": setup,
                              "lines": [{"title": "x", "quantity": "1", "unit_price": -500}]})


def test_invoice_status_only_through_payment_or_cancel(ctx, ui, setup):
    d = fin.finalize(ui, invoice(ui, setup))
    for status in ("bezahlt", "storniert", "teilbezahlt"):
        with pytest.raises(KontorError):
            docs.set_status(ctx, d["id"], status)
    assert docs.get_document(ctx, d["id"])["status"] == "abgeschlossen"


def test_abschlag_deducted_in_schluss_cannot_be_cancelled(ui, setup):
    a, s = _schluss_setup(ui, setup)
    with pytest.raises(KontorError, match=s["number"]):
        fin.cancel(ui, a["id"])


def test_mustang_validates_storno_of_schluss(ui, setup, tmp_path):
    import subprocess
    from tests.test_render import JAR, JAVA
    if not (JAVA and JAR.exists()):
        pytest.skip("Mustang-Validator nicht installiert")
    _, s = _schluss_setup(ui, setup)
    st = fin.cancel(ui, s["id"])
    out = subprocess.run([JAVA, "-jar", str(JAR), "--action", "validate", "--source", str(ui.data / st["pdf_path"]),
                          "--no-notices"], capture_output=True, text=True, timeout=180)
    assert 'status="valid"' in out.stdout and 'status="invalid"' not in out.stdout, out.stdout[-3000:]


def test_draft_preview_shows_current_customer_data(ui, setup):
    import io
    import pypdf
    did = invoice(ui, setup)
    md.update_customer(ui, setup, {"street": "Lindenallee 5"})
    text = "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(fin.preview_pdf(ui, did))).pages)
    assert "Lindenallee" in text and "Musterweg" not in text


def _offer_with_credit(ui, customer):
    oid = docs.save_draft(ui, {"kind": "angebot", "customer_id": customer, "subject": "Starterpaket",
                               "notes": "Danach 200 € im Monat.",
                               "lines": [{"title": "Entwicklung", "description": "Bestellportal", "quantity": "1",
                                          "unit_price": 500000},
                                         {"title": "Integration", "quantity": "1", "unit_price": 300000}],
                               "allowances": [{"label": "Anrechnung Analyse", "amount": -100000, "tax_rate": "19"}]})
    fin.finalize(ui, oid)
    return oid


def test_abschlag_lists_all_positions_and_applies_share(ui, setup):
    """Spec 6.4 (08.10.2026): alle Positionen, Anteil am Ende, Schlussrechnung gleich als Entwurf."""
    import io
    import pypdf
    oid = _offer_with_credit(ui, setup)
    ab = docs.get_document(ui, docs.invoice_from_offer(ui, oid, "abschlag", "50"))
    assert [l["title"] for l in ab["lines"]] == ["Entwicklung", "Integration"]
    assert [a["amount"] for a in ab["allowances"]] == [-100000, -350000]
    assert (ab["share_percent"], ab["net"], ab["gross"], ab["notes"]) == ("50", 350000, 416500, "")
    assert ab["due_date"] == (date.fromisoformat(ab["doc_date"]) + timedelta(days=14)).isoformat()
    text = "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(fin.preview_pdf(ui, ab["id"]))).pages)
    assert "Summe der Leistungen netto" in text and "davon Abschlag 50 %" in text and "Bestellportal" in text
    assert "Nicht Teil dieses Abschlags" not in text
    schluss = ui.conn.execute("SELECT id FROM documents WHERE kind = 'schluss' AND source_document_id = ?",
                              (oid,)).fetchall()
    assert len(schluss) == 1 and docs.invoice_from_offer(ui, oid, "schluss") == schluss[0][0]


def test_schluss_after_share_abschlag_deducts_prepayment(ui, setup):
    oid = _offer_with_credit(ui, setup)
    ab = docs.invoice_from_offer(ui, oid, "abschlag", "50")
    docs.save_draft(ui, {"service_date": "2026-10-08"}, ab)
    a = fin.finalize(ui, ab)
    sid = docs.invoice_from_offer(ui, oid, "schluss")
    docs.save_draft(ui, {"service_date": "2026-11-20"}, sid)
    s = fin.finalize(ui, sid)
    assert (s["net"], s["gross"], s["prepaid_gross"], s["due"]) == (700000, 833000, a["gross"], 833000 - a["gross"])
    assert a["gross"] == 416500


def test_share_recomputed_when_draft_lines_change(ui, setup):
    oid = _offer_with_credit(ui, setup)
    ab = docs.invoice_from_offer(ui, oid, "abschlag", "50")
    d = docs.get_document(ui, ab)
    lines = [{k: l[k] for k in ("title", "quantity", "unit_price", "tax_rate")} for l in d["lines"]][:1]
    docs.save_draft(ui, {"lines": lines, "allowances": [dict(a) for a in d["allowances"]]}, ab)
    d = docs.get_document(ui, ab)
    assert (d["net"], [a["amount"] for a in d["allowances"]]) == (200000, [-100000, -200000])


def test_warns_when_invoice_date_is_in_the_past(ui, setup):
    did = invoice(ui, setup, doc_date="2020-01-02")
    assert any("liegt vor heute" in w for w in fin.warnings(ui, did))


def test_mustang_validates_share_abschlag(ui, setup):
    import subprocess
    from tests.test_render import JAR, JAVA
    if not (JAVA and JAR.exists()):
        pytest.skip("Mustang-Validator nicht installiert")
    ab = docs.invoice_from_offer(ui, _offer_with_credit(ui, setup), "abschlag", "50")
    a = fin.finalize(ui, ab)     # ohne Leistungsdatum (Spec 6.4)
    assert b"OccurrenceDateTime" not in extract_xml((ui.data / a["pdf_path"]).read_bytes())
    out = subprocess.run([JAVA, "-jar", str(JAR), "--action", "validate", "--source", str(ui.data / a["pdf_path"]),
                          "--no-notices"], capture_output=True, text=True, timeout=180)
    assert 'status="valid"' in out.stdout and 'status="invalid"' not in out.stdout, out.stdout[-3000:]


def test_abschlag_needs_no_service_date_and_says_so(ui, setup):
    import io
    import pypdf
    oid = _offer_with_credit(ui, setup)
    ab = docs.invoice_from_offer(ui, oid, "abschlag", "50")
    text = "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(fin.preview_pdf(ui, ab))).pages)
    assert "Leistung noch nicht erbracht" in text and "Leistungsdatum" not in text
    docs.save_draft(ui, {"service_from": "2026-10-13", "service_to": "2026-11-30"}, ab)
    text = "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(fin.preview_pdf(ui, ab))).pages)
    assert "Voraussichtlich" in text and "13.10.2026" in text
    assert fin.finalize(ui, ab)["status"] == "abgeschlossen"
    sid = docs.invoice_from_offer(ui, oid, "schluss")
    with pytest.raises(KontorError, match="Leistungsdatum"):
        fin.finalize(ui, sid)
