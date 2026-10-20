"""Rechnungsberichtigung (§ 31 Abs. 5 UStDV): gleiche Nummer, gleiche Beträge, neue Verkäuferangaben."""
import json
import os

import pytest

from kontor.core.errors import HumanOnly, KontorError
from kontor.render.zugferd import extract_xml
from kontor.services import documents as docs
from kontor.services import finalize as fin
from kontor.services import integrity
from kontor.services import masterdata as md
from kontor.services import payments as pay
from tests.test_finalize import invoice, setup  # noqa: F401


@pytest.fixture
def pending_invoice(ui, setup):
    md.set_settings(ui, {"tax_number": "", "tax_number_pending": "1"})
    d = fin.finalize(ui, invoice(ui, setup))
    pay.record_payment(ui, d["id"], "2026-09-05", 119000)
    return d


def test_correction_adds_tax_number_keeps_number_amounts_payment(ui, pending_invoice):
    md.set_settings(ui, {"tax_number": "123/456/78901", "tax_number_pending": ""})
    c = fin.correct(ui, pending_invoice["id"], "Steuernummer ergänzt")
    assert c["version"] == 1
    pdf = (ui.data / c["pdf_path"]).read_bytes()
    assert extract_xml(pdf) is not None and b"RE-2026-001" in extract_xml(pdf)
    snap = json.loads(c["snapshot_json"])
    assert snap["seller"]["tax_number"] == "123/456/78901" and snap["correction"]["reason"] == "Steuernummer ergänzt"
    d = docs.get_document(ui, pending_invoice["id"])
    assert (d["number"], d["gross"], d["status"], d["paid"]) == ("RE-2026-001", 119000, "bezahlt", 119000)
    assert (ui.data / d["pdf_path"]).exists()                       # Original bleibt im Archiv
    assert fin.archived_pdf(ui, d["id"]) == ui.data / c["pdf_path"]  # Download liefert die berichtigte Fassung
    assert [x["version"] for x in d["corrections"]] == [1]


def test_correction_needs_a_change_and_a_human(ctx, ui, pending_invoice):
    with pytest.raises(KontorError, match="unverändert"):
        fin.correct(ui, pending_invoice["id"], "nichts")
    md.set_settings(ui, {"tax_number": "123/456/78901"})
    with pytest.raises(HumanOnly):
        fin.correct(ctx, pending_invoice["id"], "Steuernummer ergänzt")
    with pytest.raises(KontorError, match="Grund"):
        fin.correct(ui, pending_invoice["id"], "")


def test_integrity_covers_corrections(ui, pending_invoice):
    md.set_settings(ui, {"tax_number": "123/456/78901"})
    c = fin.correct(ui, pending_invoice["id"], "Steuernummer ergänzt")
    assert integrity.check(ui) == []
    path = ui.data / c["pdf_path"]
    os.chmod(path, 0o644)
    path.write_bytes(path.read_bytes() + b"x")
    assert any("Berichtigung" in p for p in integrity.check(ui))
