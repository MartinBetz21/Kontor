"""Eigenbeleg: Ausgabe ohne Rechnung, z. B. vom Privatkonto bezahlt."""
import io
import re

import pypdf
import pytest

from kontor.core.errors import KontorError
from kontor.services import masterdata as md
from kontor.services import receipts as rc

DATA = {"receipt_date": "2026-09-10", "supplier": "Example Software Inc.", "description": "Software-Abo September", "gross": "79,00",
        "tax_kind": "rc_drittland", "paid_on": "2026-09-10", "paid_via": "Privatkonto",
        "reason": "Rechnung im Kundenkonto nicht abrufbar"}


def test_own_receipt_creates_pdf_and_inbox_entry(ctx):
    r = rc.create_own_receipt(ctx, {**DATA, "category_id": md.list_categories(ctx)[0]["id"]})
    assert (r["status"], r["supplier"], r["gross"], r["tax_kind"], r["paid_on"]) == \
        ("eingang", "Example Software Inc.", 7900, "rc_drittland", "2026-09-10")
    assert "Eigenbeleg" in r["note"] and "Privatkonto" in r["note"]
    text = "".join(p.extract_text() for p in pypdf.PdfReader(io.BytesIO((ctx.data / r["file_path"]).read_bytes())).pages)
    assert "Eigenbeleg" in text and "Example Software Inc." in text and "79,00" in text


def test_own_receipt_never_claims_input_tax(ctx):
    with pytest.raises(KontorError, match="Vorsteuer"):
        rc.create_own_receipt(ctx, {**DATA, "tax_kind": "inland_19"})


def test_own_receipt_needs_reason_and_description(ctx):
    with pytest.raises(KontorError):
        rc.create_own_receipt(ctx, {**DATA, "reason": ""})


def test_own_receipt_in_ui(client):
    from tests.test_ui import setup_master
    setup_master(client)
    r = client.post("/belege/eigenbeleg", data={k: v for k, v in DATA.items()}, follow_redirects=False)
    assert r.status_code == 303 and re.search(r"/belege/\d+", r.headers["location"])


from tests.test_ui import client  # noqa: E402,F401
