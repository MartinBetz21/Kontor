"""Version 2, Schritt 2 (Spec 18.3, 18.4): Steuerarten im Ausgang, Pflichtangaben der GmbH, Nummern nach Lexware,
offene Posten aus Lexware. Alle Namen erfunden."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kontor.config import CODE_ROOT
from kontor.core.calc import Line, compute
from kontor.core.errors import KontorError
from kontor.core.snapshot import build_snapshot
from kontor.importers import lexware
from kontor.render.pdf import render_html, render_pdf
from kontor.render.zugferd import build_xml
from kontor.reports.ustva import ustva
from kontor.services import documents as docs
from kontor.services import finalize as fin
from kontor.services import integrity
from kontor.services import masterdata as md
from kontor.services import payments as pay

LOGO = Path(__file__).parent / "fixtures" / "logo-hell.svg"
JAVA = shutil.which("java", path="/opt/homebrew/opt/openjdk/bin") or shutil.which("java")
JAR = CODE_ROOT / "tools" / "Mustang-CLI.jar"
GMBH = {"name": "Muster Beratung GmbH", "street": "Hauptstraße 1", "postcode": "50667", "city": "Köln",
        "country": "DE", "email": "info@example.org", "tax_number": "214/0000/0000", "vat_id": "DE123456789",
        "iban": "DE02120300000000202051", "bic": "BYLADEM1001", "bank_name": "Testbank",
        "register_court": "Amtsgericht Köln", "register_number": "HRB 99999", "managing_directors": "Erika Muster",
        "seat": "Köln"}
SCHWEIZ = {"company": "Alpen Software AG", "contact": "Frau Test", "street": "Bahnhofstrasse 1", "postcode": "3011",
           "city": "Bern", "country": "CH"}
OESTERREICH = {"company": "Donau Digital GmbH", "street": "Ring 2", "postcode": "1010", "city": "Wien",
               "country": "AT", "vat_id": "ATU12345678"}


def snap(treatment, buyer, rate="0", seller=GMBH):
    lines = [{"position": 1, "title": "Begleitung", "description": "Monat Oktober", "quantity": "1", "unit": "",
              "unit_price": 50000, "tax_rate": rate, "total": 50000}]
    t = compute([Line("1", 50000, rate)], [])
    d = {"kind": "rechnung", "doc_date": "2026-10-05", "service_date": None, "service_from": "2026-10-01",
         "service_to": "2026-10-31", "due_date": "2026-10-12", "valid_until": None, "subject": "", "intro": "",
         "notes": "", "conditions": [], "closing": "", "lines": lines, "allowances": [], "customer": buyer,
         "tax_treatment": treatment, "net": t.net, "tax": t.tax, "gross": t.gross}
    return build_snapshot(d, seller, "RE-2026-0500", [], None)


# -- Schnappschuss, PDF, E-Rechnung ------------------------------------------
def test_third_country_invoice_carries_the_note_and_no_tax():
    s = snap("drittland_b2b", SCHWEIZ)
    assert s["tax_treatment"] == "drittland_b2b" and s["totals"]["gross"] == 50000
    html = render_html(s, LOGO, draft=False)
    assert "Nicht im Inland steuerbare Leistung (§ 3a Abs. 2 UStG)" in html
    assert "Die Steuer schuldet der Leistungsempfänger." in html
    assert "zzgl. 0 %" not in html
    assert "Schweiz" in html.split('<div class="address">')[1].split("</div>")[0]


def test_eu_reverse_charge_names_both_vat_ids():
    html = render_html(snap("eu_b2b_rc", OESTERREICH), LOGO, draft=False)
    assert "Steuerschuldnerschaft des Leistungsempfängers" in html
    assert "ATU12345678" in html and "DE123456789" in html


def test_gmbh_footer_has_register_and_managing_director():
    html = render_html(snap("inland", {**SCHWEIZ, "country": "DE"}, rate="19"), LOGO, draft=False)
    foot = html.split('<div class="foot">')[1]
    assert "Amtsgericht Köln" in foot and "HRB 99999" in foot and "Geschäftsführer: Erika Muster" in foot
    assert "Sitz: Köln" in foot


def test_xml_categories():
    o = build_xml(snap("drittland_b2b", SCHWEIZ))
    assert b"<ram:CategoryCode>O</ram:CategoryCode>" in o and b"ExemptionReason" in o
    assert b"<ram:RateApplicablePercent>" not in o
    ae = build_xml(snap("eu_b2b_rc", OESTERREICH))
    assert b"<ram:CategoryCode>AE</ram:CategoryCode>" in ae and b"ATU12345678" in ae


@pytest.mark.skipif(not (JAVA and JAR.exists()), reason="Mustang-Validator nicht installiert")
@pytest.mark.parametrize("treatment,buyer", [("drittland_b2b", SCHWEIZ), ("eu_b2b_rc", OESTERREICH)])
def test_mustang_validates_foreign_treatments(tmp_path, treatment, buyer):
    path = tmp_path / f"{treatment}.pdf"
    path.write_bytes(render_pdf(snap(treatment, buyer), LOGO))
    out = subprocess.run([JAVA, "-jar", str(JAR), "--action", "validate", "--source", str(path), "--no-notices"],
                         capture_output=True, text=True, timeout=180)
    assert 'status="valid"' in out.stdout and 'status="invalid"' not in out.stdout, out.stdout[-3000:]


# -- Entwurf und Abschließen -------------------------------------------------
@pytest.fixture
def gmbh(ctx):
    (ctx.data / "logo-hell.svg").write_bytes(LOGO.read_bytes())
    md.set_settings(ctx, {**GMBH, "logo_path": "logo-hell.svg"})
    md.set_profile(ctx, {"gewinnermittlung": "bilanz", "nummernstellen": "4"})
    return {"ch": md.create_customer(ctx, SCHWEIZ), "at": md.create_customer(ctx, OESTERREICH),
            "de": md.create_customer(ctx, {**SCHWEIZ, "country": "DE", "company": "Inland GmbH"})}


def draft(ctx, customer, treatment, rate="19"):
    return docs.save_draft(ctx, {"kind": "rechnung", "customer_id": customer, "tax_treatment": treatment,
                                 "doc_date": "2026-10-05", "service_from": "2026-10-01", "service_to": "2026-10-31",
                                 "lines": [{"title": "Begleitung", "quantity": "1", "unit_price": 50000,
                                            "tax_rate": rate}]})


def test_foreign_treatment_forces_zero_rate(ctx, gmbh):
    d = docs.get_document(ctx, draft(ctx, gmbh["ch"], "drittland_b2b", rate="19"))
    assert d["tax_treatment"] == "drittland_b2b" and d["lines"][0]["tax_rate"] == "0" and d["gross"] == 50000


def test_unknown_treatment_is_refused(ctx, gmbh):
    with pytest.raises(KontorError):
        draft(ctx, gmbh["ch"], "irgendwas")


def test_third_country_needs_a_foreign_non_eu_business(ui, gmbh):
    with pytest.raises(KontorError, match="Drittland"):
        fin.finalize(ui, draft(ui, gmbh["de"], "drittland_b2b"))
    with pytest.raises(KontorError, match="Drittland"):
        fin.finalize(ui, draft(ui, gmbh["at"], "drittland_b2b"))
    assert fin.finalize(ui, draft(ui, gmbh["ch"], "drittland_b2b"))["gross"] == 50000


def test_eu_reverse_charge_needs_both_vat_ids(ui, gmbh):
    no_id = md.create_customer(ui, {**OESTERREICH, "vat_id": ""})
    with pytest.raises(KontorError, match="USt-IdNr. des Kunden"):
        fin.finalize(ui, draft(ui, no_id, "eu_b2b_rc"))
    md.set_settings(ui, {"vat_id": ""})
    with pytest.raises(KontorError, match="eigene USt-IdNr."):
        fin.finalize(ui, draft(ui, gmbh["at"], "eu_b2b_rc"))


def test_balance_sheet_profile_needs_register_data(ui, gmbh):
    md.set_settings(ui, {"register_number": ""})
    with pytest.raises(KontorError, match="Handelsregister"):
        fin.finalize(ui, draft(ui, gmbh["de"], "inland"))


def test_storno_keeps_the_treatment(ui, gmbh):
    did = fin.finalize(ui, draft(ui, gmbh["ch"], "drittland_b2b"))["id"]
    storno = fin.cancel(ui, did)
    assert storno["tax_treatment"] == "drittland_b2b"


# -- Voranmeldung -------------------------------------------------------------
def test_ustva_reports_kz45_and_kz21_in_the_service_month(ui, gmbh):
    ch = fin.finalize(ui, draft(ui, gmbh["ch"], "drittland_b2b"))
    fin.finalize(ui, draft(ui, gmbh["at"], "eu_b2b_rc"))
    pay.record_payment(ui, ch["id"], "2026-11-03", 50000)
    oct_, nov = ustva(ui, "2026-10"), ustva(ui, "2026-11")
    assert oct_["fields"]["45"]["basis"] == 500 and oct_["fields"]["21"]["basis"] == 500
    assert nov["fields"]["45"]["basis"] == 0 and nov["fields"]["81"]["basis"] == 0 and nov["zahllast"] == 0


def test_ustva_for_a_whole_year(ui, gmbh):
    fin.finalize(ui, draft(ui, gmbh["ch"], "drittland_b2b"))
    assert ustva(ui, "2026")["fields"]["45"]["basis"] == 500


# -- Nummern nach Lexware (Spec 18.3) -----------------------------------------
def test_counter_continues_after_the_last_lexware_number(ui, gmbh):
    md.set_counter_start(ui, "RE", 499)
    assert fin.finalize(ui, draft(ui, gmbh["de"], "inland"))["number"] == "RE-2026-0500"
    assert integrity.check(ui) == []
    with pytest.raises(KontorError, match="schon Nummern"):
        md.set_counter_start(ui, "RE", 600)


# -- Offene Posten aus Lexware (Spec 18.3) ------------------------------------
def lexware_invoice(number="RE-2024-0017", net=1200000, rate=19, gross=1428000):
    return {"voucherNumber": number, "voucherDate": "2024-10-14T00:00:00.000+02:00",
            "dueDate": "2024-10-28T00:00:00.000+01:00", "title": "Rechnung", "introduction": "Wie besprochen.",
            "remark": "Danke.", "voucherStatus": "open",
            "address": {"name": "Beispiel Consulting Inh. Max Test", "street": "Testweg 3", "zip": "50674",
                        "city": "Köln", "countryCode": "DE"},
            "shippingConditions": {"shippingDate": "2024-10-01T00:00:00.000+02:00",
                                   "shippingEndDate": "2024-10-31T23:59:59.999+01:00", "shippingType": "deliveryperiod"},
            "taxConditions": {"taxType": "net"},
            "lineItems": [{"type": "custom", "name": "Workshop", "description": "Unterstützung", "quantity": 1,
                           "unitName": "Stück", "unitPrice": {"netAmount": net / 100, "taxRatePercentage": rate}}],
            "totalPrice": {"totalNetAmount": net / 100, "totalGrossAmount": gross / 100,
                           "totalTaxAmount": (gross - net) / 100}}


PAYMENTS = {"paymentItems": [
    {"paymentItemType": "partPaymentFinancialTransaction", "amount": 5000, "postingDate": "2024-10-30T01:00:00.000+01:00"},
    {"paymentItemType": "partPaymentFinancialTransaction", "amount": 3000, "postingDate": "2025-01-20T09:12:00.000+01:00"}]}


def test_import_open_lexware_invoice(ctx, gmbh):
    did = lexware.import_invoice(ctx, lexware_invoice(), PAYMENTS, b"%PDF-1.4 lexware")
    d = docs.get_document(ctx, did)
    assert (d["number"], d["status"], d["origin"], d["gross"], d["open"]) == \
        ("RE-2024-0017", "teilbezahlt", "lexware", 1428000, 628000)
    assert (ctx.data / d["pdf_path"]).read_bytes() == b"%PDF-1.4 lexware"
    assert d["service_from"] == "2024-10-01" and d["service_to"] == "2024-10-31"
    assert integrity.check(ctx) == []


def test_lexware_payments_stay_out_of_the_ustva_but_new_ones_count(ctx, ui, gmbh):
    did = lexware.import_invoice(ctx, lexware_invoice(), PAYMENTS, b"%PDF")
    assert ustva(ctx, "2024-10")["fields"]["81"]["basis"] == 0     # schon über Lexware erklärt
    pay.record_payment(ui, did, "2026-10-20", 628000)              # Ist-Versteuerung: fällig mit dem Eingang
    assert ustva(ui, "2026-10")["fields"]["81"]["basis"] == 5277
    assert docs.get_document(ui, did)["status"] == "bezahlt"


def test_import_refuses_totals_that_do_not_match(ctx, gmbh):
    with pytest.raises(KontorError, match="Summe"):
        lexware.import_invoice(ctx, lexware_invoice(gross=1427997), PAYMENTS, b"%PDF")


def test_import_refuses_a_number_twice(ctx, gmbh):
    lexware.import_invoice(ctx, lexware_invoice(), PAYMENTS, b"%PDF")
    with pytest.raises(KontorError, match="RE-2024-0017"):
        lexware.import_invoice(ctx, lexware_invoice(), PAYMENTS, b"%PDF")


def test_import_third_country_invoice(ctx, gmbh):
    inv = lexware_invoice("RE-2026-0912", net=50000, rate=0, gross=50000)
    inv["taxConditions"] = {"taxType": "thirdPartyCountryService", "taxTypeNote": "Nicht im Inland steuerbare Leistung"}
    inv["address"] = {"name": "Alpen Software AG", "street": "Bahnhofstrasse 1", "zip": "3011", "city": "Bern",
                      "countryCode": "CH"}
    d = docs.get_document(ctx, lexware.import_invoice(ctx, inv, {"paymentItems": []}, b"%PDF"))
    assert (d["tax_treatment"], d["status"], d["open"]) == ("drittland_b2b", "abgeschlossen", 50000)
    assert json.loads(d["snapshot_json"])["origin"] == "lexware"


def test_import_without_payments_and_voided(ctx, gmbh):
    d = docs.get_document(ctx, lexware.import_invoice(ctx, lexware_invoice(), PAYMENTS, b"%PDF", with_payments=False))
    assert (d["status"], d["paid"]) == ("abgeschlossen", 0)
    v = lexware_invoice("RE-2026-0913", net=50000, rate=19, gross=59500)
    v["voucherStatus"] = "voided"
    assert docs.get_document(ctx, lexware.import_invoice(ctx, v, {"paymentItems": []}, b"%PDF"))["status"] == "storniert"


def test_import_matches_customer_by_lexware_contact_and_keeps_invoice_address(ctx, gmbh):
    a = lexware_invoice("RE-2026-0001")
    a["address"]["contactId"] = "c-1"
    first = docs.get_document(ctx, lexware.import_invoice(ctx, a, {"paymentItems": []}, b"%PDF"))
    b = lexware_invoice("RE-2026-0002")
    b["address"] = {**a["address"], "name": "Neuer Name GmbH"}
    second = docs.get_document(ctx, lexware.import_invoice(ctx, b, {"paymentItems": []}, b"%PDF"))
    assert first["customer_id"] == second["customer_id"]
    assert json.loads(second["snapshot_json"])["buyer"]["company"] == "Neuer Name GmbH"


def test_imported_lexware_invoice_of_an_open_year_counts_in_kz45(ctx, gmbh):
    inv = lexware_invoice("RE-2026-0916", net=50000, rate=0, gross=50000)
    inv["taxConditions"] = {"taxType": "thirdPartyCountryService"}
    inv["shippingConditions"] = {"shippingDate": "2026-03-01T00:00:00.000+01:00",
                                 "shippingEndDate": "2026-03-31T00:00:00.000+01:00", "shippingType": "deliveryperiod"}
    inv["voucherDate"] = "2026-03-05T00:00:00.000+01:00"
    lexware.import_invoice(ctx, inv, {"paymentItems": []}, b"%PDF", with_payments=False)
    voided = {**inv, "voucherNumber": "RE-2026-0913", "voucherStatus": "voided"}
    lexware.import_invoice(ctx, voided, {"paymentItems": []}, b"%PDF2", with_payments=False)
    assert ustva(ctx, "2026-03")["fields"]["45"]["basis"] == 500
