"""Zahlungshinweis auf Rechnungen: sichtbar und änderbar im Editor, Vorlage in den Einstellungen."""
import io

import pypdf
import pytest

from kontor.core.errors import KontorError
from kontor.services import documents as docs
from kontor.services import finalize as fin
from kontor.services import masterdata as md
from tests.test_finalize import invoice, setup  # noqa: F401
from tests.test_ui import client, setup_master  # noqa: F401
from tests.test_ui_documents import doc_id, invoice_form


def pdf_text(pdf: bytes) -> str:
    return " ".join(" ".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO(pdf)).pages).split())


def test_new_invoice_gets_default_note_with_placeholders(ui, setup):
    did = invoice(ui, setup)
    assert docs.get_document(ui, did)["payment_note"] == md.get_settings(ui)["payment_note_template"]
    text = pdf_text(fin.preview_pdf(ui, did))
    assert "Bitte überweisen Sie 1.190,00 € bis 16.10.2026 an " in text and "IBAN DE89370400440532013000" in text


def test_note_is_editable_and_empty_means_none(ui, setup):
    did = invoice(ui, setup)
    docs.save_draft(ui, {"payment_note": "Bitte überweise {betrag} bis {faellig_am}."}, did)
    assert "Bitte überweise 1.190,00 € bis 16.10.2026." in pdf_text(fin.preview_pdf(ui, did))
    docs.save_draft(ui, {"payment_note": ""}, did)
    assert "überweis" not in pdf_text(fin.preview_pdf(ui, did))


def test_note_is_locked_after_finalize(ui, setup):
    d = fin.finalize(ui, invoice(ui, setup))
    with pytest.raises(KontorError):
        docs.save_draft(ui, {"payment_note": "x"}, d["id"])
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        ui.conn.execute("UPDATE documents SET payment_note = 'x' WHERE id = ?", (d["id"],))


def test_offer_has_no_payment_note(ui, setup):
    oid = docs.save_draft(ui, {"kind": "angebot", "customer_id": setup, "lines": []})
    assert docs.get_document(ui, oid)["payment_note"] is None


def test_editor_shows_payment_note_field(client):
    cid = setup_master(client)
    did = doc_id(client.post("/dokumente", data=invoice_form(cid), follow_redirects=False))
    page = client.get(f"/dokumente/{did}").text
    assert 'name="payment_note"' in page and "{betrag}" in page
    client.post(f"/dokumente/{did}", data={**invoice_form(cid), "payment_note": "Zahlbar bis {faellig_am}."})
    assert "Zahlbar bis {faellig_am}." in client.get(f"/dokumente/{did}").text
