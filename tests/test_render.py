import io
import shutil
import subprocess
from pathlib import Path

import pypdf
import pytest

from kontor.config import CODE_ROOT
from kontor.core.calc import Allowance, Line, compute
from kontor.core.errors import KontorError
from kontor.core.snapshot import build_snapshot, fill_placeholders
from kontor.render.pdf import render_html, render_pdf
from kontor.render.zugferd import build_xml, extract_xml, read_xml, unit_code

LOGO = Path(__file__).parent / "fixtures" / "logo-hell.svg"
SETTINGS = {"name": "Erika Mustermann", "trade_name": "Example Consulting", "street": "Musterstraße 1", "postcode": "10115",
            "city": "Berlin", "country": "DE", "email": "info@example.com", "phone": "+49 30 1234567",
            "tax_number": "123/456/78901", "vat_id": "", "iban": "DE89 3704 0044 0532 0130 00", "bic": "COBADEFFXXX",
            "bank_name": "Testbank"}


def doc(kind="rechnung", lines=None, allowances=None):
    lines = lines or [{"position": 1, "title": "Prozessanalyse Auftragsabwicklung", "description": "Ä Ö Ü ß €",
                       "quantity": "1", "unit": "psch.", "unit_price": 100000, "tax_rate": "19", "total": 100000}]
    allowances = allowances or []
    t = compute([Line(l["quantity"], l["unit_price"], l["tax_rate"]) for l in lines],
                [Allowance(a["amount"], a["tax_rate"]) for a in allowances])
    return {"kind": kind, "doc_date": "2026-10-02", "service_date": "2026-09-30", "service_from": None,
            "service_to": None, "due_date": "2026-10-16", "valid_until": None, "subject": "Prozessanalyse",
            "intro": "Hallo {vorname},", "notes": "", "conditions_json": "[]", "conditions": [],
            "closing": "Zahlbar bis {faellig_am}.", "lines": lines, "allowances": allowances,
            "customer": {"company": "Beispiel Technik GmbH & Co. KG", "contact": "Max Beispiel", "street": "Musterweg 1",
                         "postcode": "10115", "city": "Berlin", "country": "DE"},
            "net": t.net, "tax": t.tax, "gross": t.gross}


def test_snapshot_fills_placeholders_and_totals():
    s = build_snapshot(doc(), SETTINGS, "RE-2026-001", [], None)
    assert s["format_version"] == 1 and s["number"] == "RE-2026-001"
    assert s["intro"] == "Hallo Max," and s["closing"] == "Zahlbar bis 16.10.2026."
    assert s["totals"]["gross"] == 119000 and s["due"] == 119000
    assert fill_placeholders("{unbekannt}", s) == "{unbekannt}"


def test_snapshot_prepaid_reduces_due():
    s = build_snapshot(doc("schluss"), SETTINGS, "RE-2026-004",
                       [{"number": "RE-2026-002", "rate": "19", "net": 50000, "tax": 9500, "gross": 59500}], None)
    assert s["due"] == 119000 - 59500


def test_xml_validates_and_roundtrips():
    s = build_snapshot(doc(allowances=[{"position": 1, "label": "Anrechnung", "amount": -10000, "tax_rate": "19"}]),
                       SETTINGS, "RE-2026-001", [], None)
    parsed = read_xml(build_xml(s))   # build_xml validiert gegen das XSD (drafthorse wirft sonst)
    assert parsed == {"supplier": "Erika Mustermann · Example Consulting", "receipt_number": "RE-2026-001",
                      "receipt_date": "2026-10-02", "net": 90000, "tax": 17100, "gross": 107100, "tax_rates": ["19"]}


def test_storno_xml_is_credit_note_with_reference():
    d = doc("storno", lines=[{"position": 1, "title": "Prozessanalyse", "description": "", "quantity": "1",
                              "unit": "psch.", "unit_price": -100000, "tax_rate": "19", "total": -100000}])
    xml = build_xml(build_snapshot(d, SETTINGS, "RE-2026-005", [], "RE-2026-001"))
    assert b"<ram:TypeCode>381</ram:TypeCode>" in xml and b"RE-2026-001" in xml
    assert read_xml(xml)["gross"] == -119000


def test_invoice_pdf_embeds_xml():
    s = build_snapshot(doc(), SETTINGS, "RE-2026-001", [], None)
    pdf = render_pdf(s, LOGO)
    xml = extract_xml(pdf)
    assert xml is not None and read_xml(xml)["gross"] == 119000
    meta = pypdf.PdfReader(io.BytesIO(pdf)).xmp_metadata
    assert meta is not None


def test_offer_pdf_has_no_xml_and_draft_is_marked():
    s = build_snapshot(doc("angebot"), SETTINGS, None, [], None)
    assert extract_xml(render_pdf(s, LOGO)) is None
    assert "Entwurf" in render_html(s, LOGO, draft=True)


def test_many_lines_with_umlauts_render_multipage():
    lines = [{"position": i, "title": f"Leistung {i} – Größenänderung für Übergänge", "description": "Ä Ö Ü ß € " * 20,
              "quantity": "1", "unit": "Std.", "unit_price": 12000, "tax_rate": "19", "total": 12000}
             for i in range(1, 31)]
    s = build_snapshot(doc(lines=lines), SETTINGS, "RE-2026-009", [], None)
    pdf = render_pdf(s, LOGO)
    assert len(pypdf.PdfReader(io.BytesIO(pdf)).pages) >= 2
    assert read_xml(extract_xml(pdf))["net"] == 360000


def test_missing_logo_is_a_clear_error(tmp_path):
    s = build_snapshot(doc(), SETTINGS, "RE-2026-001", [], None)
    with pytest.raises(KontorError, match="Logo"):
        render_pdf(s, tmp_path / "fehlt.svg")


def test_unit_codes():
    assert (unit_code("psch."), unit_code("Monate"), unit_code("Std."), unit_code("Lizenz")) == ("LS", "MON", "HUR", "C62")

JAVA = shutil.which("java", path="/opt/homebrew/opt/openjdk/bin") or shutil.which("java")
JAR = CODE_ROOT / "tools" / "Mustang-CLI.jar"


@pytest.mark.skipif(not (JAVA and JAR.exists()), reason="Mustang-Validator nicht installiert (siehe Task 7, Step 7)")
def test_mustang_validates_invoice_pdf(tmp_path):
    s = build_snapshot(doc(allowances=[{"position": 1, "label": "Anrechnung", "amount": -10000, "tax_rate": "19"}]),
                       SETTINGS, "RE-2026-001", [], None)
    path = tmp_path / "RE-2026-001.pdf"
    path.write_bytes(render_pdf(s, LOGO))
    out = subprocess.run([JAVA, "-jar", str(JAR), "--action", "validate", "--source", str(path), "--no-notices"],
                         capture_output=True, text=True, timeout=180)
    assert 'status="valid"' in out.stdout and 'status="invalid"' not in out.stdout, out.stdout[-3000:]


def test_seller_identifier_without_vat_id():
    # BR-CO-26: ohne USt-IdNr. braucht die E-Rechnung eine Verkäuferkennung (BT-29) — hier die Steuernummer
    xml = build_xml(build_snapshot(doc(), SETTINGS, "RE-2026-001", [], None))
    seller = xml.split(b"<ram:SellerTradeParty>")[1].split(b"</ram:SellerTradeParty>")[0]
    assert b"<ram:ID>123/456/78901</ram:ID>" in seller


@pytest.mark.skipif(not (JAVA and JAR.exists()), reason="Mustang-Validator nicht installiert (siehe Task 7, Step 7)")
@pytest.mark.parametrize("kind", ["storno", "schluss"])
def test_mustang_validates_storno_and_schluss(tmp_path, kind):
    sign = -1 if kind == "storno" else 1
    lines = [{"position": 1, "title": "Entwicklung", "description": "", "quantity": "1", "unit": "psch.",
              "unit_price": sign * 800000, "tax_rate": "19", "total": sign * 800000}]
    prepaid = [] if kind == "storno" else [{"number": "RE-2026-002", "rate": "19", "net": 400000, "tax": 76000,
                                            "gross": 476000}]
    s = build_snapshot(doc(kind, lines=lines), SETTINGS, "RE-2026-005", prepaid,
                       "RE-2026-001" if kind == "storno" else "AN-2026-001")
    path = tmp_path / f"{kind}.pdf"
    path.write_bytes(render_pdf(s, LOGO))
    out = subprocess.run([JAVA, "-jar", str(JAR), "--action", "validate", "--source", str(path), "--no-notices"],
                         capture_output=True, text=True, timeout=180)
    assert 'status="valid"' in out.stdout and 'status="invalid"' not in out.stdout, out.stdout[-3000:]


def test_included_position_shows_inklusive():
    lines = [{"position": 1, "title": "Entwicklung", "description": "", "quantity": "1", "unit": "psch.",
              "unit_price": 500000, "tax_rate": "19", "total": 500000},
             {"position": 2, "title": "Basispaket", "description": "", "quantity": "1", "unit": "psch.",
              "unit_price": 0, "tax_rate": "19", "total": 0}]
    html = render_html(build_snapshot(doc("angebot", lines=lines), SETTINGS, None, [], None), LOGO, draft=True)
    row = html.split("Basispaket")[1].split("</tr>")[0]
    assert "inklusive" in row and "0,00" not in row


def test_totals_only_after_the_last_position():
    lines = [{"position": i, "title": f"Leistung {i}", "description": "Beschreibung " * 40, "quantity": "1",
              "unit": "psch.", "unit_price": 10000, "tax_rate": "19", "total": 10000} for i in range(1, 9)]
    pdf = render_pdf(build_snapshot(doc(lines=lines), SETTINGS, "RE-2026-009", [], None), LOGO)
    pages = [p.extract_text() for p in pypdf.PdfReader(io.BytesIO(pdf)).pages]
    assert len(pages) >= 2
    assert all("Gesamtbetrag" not in p for p in pages[:-1]) and "Gesamtbetrag" in pages[-1]


def test_footer_has_four_tidy_columns():
    html = render_html(build_snapshot(doc(), SETTINGS, "RE-2026-001", [], None), LOGO, draft=False)
    foot = html.split('<div class="foot">')[1].split("</footer>")[0] if "</footer>" in html else html.split('<div class="foot">')[1]
    assert foot.count('class="foot-col"') == 4
    assert f"Kontoinhaber {SETTINGS['name']}" in foot
    assert "IBAN DE89" in foot and "Steuernummer 123/456/78901" in foot and "info@example.com" in foot


def test_pdf_shows_quantity_without_unit():
    lines = [{"position": 1, "title": "Einführung", "description": "", "quantity": "3", "unit": "Monate",
              "unit_price": 50000, "tax_rate": "19", "total": 150000}]
    html = render_html(build_snapshot(doc(lines=lines), SETTINGS, "RE-2026-001", [], None), LOGO, draft=False)
    row = html.split("Einführung</b>")[1].split("</tr>")[0]
    assert "Monate" not in row and ">3<" in row.replace(" ", "")


@pytest.mark.skipif(not (JAVA and JAR.exists()), reason="Mustang-Validator nicht installiert")
def test_png_logo_keeps_pdfa_valid(tmp_path):
    """Rastergrafiken bekommen sonst /Interpolate true, das verletzt PDF/A (ISO 19005-3, 6.2.8)."""
    import struct
    import zlib
    raw = b"".join(b"\x00" + bytes([40, 53, 131]) * 8 for _ in range(8))
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 8, 8, 8, 2, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    logo = tmp_path / "logo.png"
    logo.write_bytes(png)
    path = tmp_path / "RE-2026-001.pdf"
    path.write_bytes(render_pdf(build_snapshot(doc(), SETTINGS, "RE-2026-001", [], None), logo))
    out = subprocess.run([JAVA, "-jar", str(JAR), "--action", "validate", "--source", str(path), "--no-notices"],
                         capture_output=True, text=True, timeout=180)
    assert 'status="valid"' in out.stdout and 'status="invalid"' not in out.stdout, out.stdout[-3000:]
