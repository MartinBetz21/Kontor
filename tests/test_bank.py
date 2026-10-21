"""Bank als Quelle der Buchungen (Spec 18.7a). Alle Namen und IBANs erfunden bis auf die Fallen-Muster."""
import pytest

from kontor.core.errors import KontorError
from kontor.importers.datev import parse_datev
from kontor.reports.abschluss import auffaelligkeiten, bilanz_guv
from kontor.services import bank
from kontor.services import documents as docs
from kontor.services import finalize as fin
from kontor.services import journal as jr
from kontor.services import masterdata as md

HAUPT, STEUER, FREMD = "DE47123456782222222222", "DE97123456783333333333", "DE21876543214444444444"


def line(iban, day, amount, name, purpose="", cp=""):
    return f'"TESTDEFFXXX";"{iban}";;;{day};{day};{amount};"{name}";;;"{cp}";"{purpose}";"";"";"";;"EUR";"x";"x";'


DATEV = "\n".join([
    line(HAUPT, "03.01.2026", "-29,90", "GOOGLE*WORKSPACE EXAMPLE", "VISA GOOGLE.COM"),
    line(HAUPT, "08.01.2026", "-59,50", "Haufe Service Center GmbH", "Lexware Office XL", FREMD),
    line(HAUPT, "03.01.2026", "-29,90", "GOOGLE*WORKSPACE EXAMPLE", "VISA GOOGLE.COM"),          # Exportdublette
    line(STEUER, "22.01.2026", "-48,50", "Erika Mustermann", "Umbuchung"),                           # Abgang trägt den Inhabernamen
    line(HAUPT, "22.01.2026", "48,50", STEUER, "Umbuchung"),                                     # Zugang mit eigener IBAN
    line(HAUPT, "19.03.2026", "500,00", "Kunde Beispiel", "RE-2026-0501"),
    line(HAUPT, "05.02.2026", "-119,00", "Mustermann Leasing GmbH", "Leasingrate"),
])

SALDEN = """Konto;Bezeichnung;Saldo;S/H
1813;Hauptkonto;600,00;S
1809;Steuerkonto;2000,00;S
2900;Gezeichnetes Kapital;12500,00;H
2978;Verlustvortrag;9900,00;S
"""


@pytest.fixture
def gmbh(ctx):
    md.set_profile(ctx, {"gewinnermittlung": "bilanz", "kontenrahmen": "skr04", "nummernstellen": "4"})
    md.set_settings(ctx, {"share_capital": "25000,00", "share_capital_outstanding": "12500,00"})
    bank.add_account(ctx, HAUPT, "Hauptkonto", "1813")
    bank.add_account(ctx, STEUER, "Steuerkonto", "1809")
    jr.opening_from_saldenliste(ctx, 2026, SALDEN)
    return ctx


def test_parser_takes_valuta_marks_duplicates_and_own_ibans():
    rows = parse_datev(DATEV, {HAUPT, STEUER}, [])
    assert [r["duplicate"] for r in rows] == [False, False, True, False, False, False, False]
    assert rows[1]["counterparty_iban"] == FREMD and rows[1]["date"] == "2026-01-08"
    assert rows[4]["internal"] and not rows[3]["internal"]                    # Abgang mit Inhabername erst über die Paarung


def test_import_is_idempotent_and_pairs_transfers_both_ways(gmbh):
    first = bank.import_datev(gmbh, DATEV)
    assert (first["neu"], first["dubletten"], first["paare_neu"]) == (7, 1, 1)
    assert bank.import_datev(gmbh, DATEV)["neu"] == 0
    intern = [t for t in bank.transactions(gmbh, 2026) if t["kind"] == "intern"]
    assert len(intern) == 2 and {t["amount"] for t in intern} == {-4850, 4850}


def test_unknown_bank_account_is_refused(ctx):
    md.set_profile(ctx, {"gewinnermittlung": "bilanz"})
    with pytest.raises(KontorError, match="Unbekanntes Bankkonto"):
        bank.import_datev(ctx, DATEV)


def by_name(ctx, name):
    return next(t for t in bank.transactions(ctx, 2026) if name in t["counterparty"] and not t["duplicate"])


def test_assignment_books_net_vat_and_reverse_charge(gmbh):
    bank.import_datev(gmbh, DATEV)
    bank.assign(gmbh, by_name(gmbh, "Haufe")["id"], "konto", account="6830", tax_key="19")
    bank.assign(gmbh, by_name(gmbh, "GOOGLE")["id"], "konto", account="6837", tax_key="rc_eu")
    bal = jr.balances(gmbh, 2026)
    assert bal["6830"] == 5000 and bal["1406"] == 950                               # 59,50 = 50,00 + 9,50
    assert bal["6837"] == 2990 and bal["1407"] == 568 and bal["3837"] == -568       # § 13b, zahllastneutral
    assert "1460" not in bal                                                         # Umbuchung hebt sich auf


def test_one_target_per_transaction(gmbh):
    bank.import_datev(gmbh, DATEV)
    t = by_name(gmbh, "Haufe")["id"]
    bank.assign(gmbh, t, "konto", account="6830", tax_key="19")
    with pytest.raises(KontorError, match="schon zugeordnet"):
        bank.assign(gmbh, t, "konto", account="6830", tax_key="19")


def test_duplicate_counts_only_after_confirmation(gmbh):
    bank.import_datev(gmbh, DATEV)
    dup = next(t for t in bank.transactions(gmbh, 2026) if t["duplicate"])
    with pytest.raises(KontorError, match="Dublette"):
        bank.assign(gmbh, dup["id"], "konto", account="6837", tax_key="rc_eu")
    assert any(f["art"] == "bank_dublette" for f in auffaelligkeiten(gmbh, 2026))
    bank.confirm_duplicate(gmbh, dup["id"], "Kontoauszug zeigt zwei Abbuchungen")
    assert not any(f["art"] == "bank_dublette" for f in auffaelligkeiten(gmbh, 2026))


@pytest.fixture
def invoice(gmbh, ui):
    md.set_settings(ui, {"name": "Muster GmbH", "street": "Weg 1", "postcode": "50667", "city": "Köln",
                         "tax_number": "214/0000/0000", "seat": "Köln", "register_court": "Amtsgericht Köln",
                         "register_number": "HRB 1", "managing_directors": "Erika Muster"})
    md.set_counter_start(ui, "RE", 500)
    cid = md.create_customer(ui, {"company": "Alpen AG", "street": "Gasse 1", "postcode": "3011", "city": "Bern",
                                  "country": "CH"})
    did = docs.save_draft(ui, {"kind": "rechnung", "customer_id": cid, "tax_treatment": "drittland_b2b",
                               "doc_date": "2026-03-02", "service_from": "2026-03-01", "service_to": "2026-03-31",
                               "lines": [{"title": "Begleitung", "quantity": "1", "unit_price": 50000}]})
    return fin.finalize(ui, did)


def test_invoice_and_its_bank_payment(gmbh, invoice):
    assert invoice["number"] == "RE-2026-0501"
    bank.import_datev(gmbh, DATEV)
    assert bank.apply_suggestions(gmbh, 2026) >= 1                                   # Nummer im Verwendungszweck
    assert docs.get_document(gmbh, invoice["id"])["status"] == "bezahlt"
    bal = jr.balances(gmbh, 2026)
    assert bal["4338"] == -50000 and "1200" not in bal                               # Erlös Drittland, Forderung beglichen


def test_inland_invoice_moves_vat_on_payment(gmbh, ui, invoice):
    cid = md.create_customer(ui, {"company": "Inland GmbH", "street": "Weg 2", "postcode": "50667", "city": "Köln"})
    did = fin.finalize(ui, docs.save_draft(ui, {"kind": "rechnung", "customer_id": cid, "doc_date": "2026-03-02",
                                                "service_date": "2026-03-02",
                                                "lines": [{"title": "Beratung", "quantity": "1", "unit_price": 10000}]}))["id"]
    assert jr.balances(gmbh, 2026)["3816"] == -1900                                  # nicht fällig bis zur Zahlung
    from kontor.services import payments as pay
    pay.record_payment(ui, did, "2026-04-01", 11900)
    bal = jr.balances(gmbh, 2026)
    assert bal.get("3816", 0) == 0 and bal["3806"] == -1900
    assert any(f["art"] == "zahlung_ohne_bank" for f in auffaelligkeiten(gmbh, 2026))


def test_unassigned_transactions_park_on_3500_and_are_listed(gmbh):
    bank.import_datev(gmbh, DATEV)
    bal = jr.balances(gmbh, 2026)
    assert bal["1813"] == 60000 - 2990 - 5950 + 4850 + 50000 - 11900
    found = {f["art"]: f for f in auffaelligkeiten(gmbh, 2026)}
    assert found["ohne_zuordnung"]["betrag"] == -2990 - 5950 + 50000 - 11900


def test_anchor_shows_the_difference(gmbh):
    bank.import_datev(gmbh, DATEV)
    bank.set_anchor(gmbh, HAUPT, "2026-01-31", "528,10")                             # 600,00 − 29,90 − 59,50 + 48,50 − 31,00
    f = next(f for f in auffaelligkeiten(gmbh, 2026) if f["art"] == "saldo_anker")
    assert f["betrag"] == -3100
    bank.set_anchor(gmbh, HAUPT, "2026-01-31", "559,10")
    assert not any(f["art"] == "saldo_anker" for f in auffaelligkeiten(gmbh, 2026))


def test_rules_learned_from_lexware_and_foreign_providers_get_reverse_charge(gmbh):
    kon = ('"Konto";"Kontobezeichnung";"Datum";"Belegnummer";"Text";"Gegenkonto";"Soll";"Haben";"Steuer";"Steuerkonto"\n'
           '"1813";"Hauptkonto";"08.01.2025";"";"Haufe Service Center GmbH";"6830";"0,00";"59,50";"";""\n'
           '"6830";"Buchführung";"08.01.2025";"";"Haufe Service Center GmbH";"1813";"50,00";"0,00";"19,00";"1406"\n'
           '"1813";"Hauptkonto";"08.02.2025";"";"Haufe Service Center GmbH";"6830";"0,00";"59,50";"";""\n'
           '"6830";"Buchführung";"08.02.2025";"";"Haufe Service Center GmbH";"1813";"50,00";"0,00";"19,00";"1406"\n'
           '"1813";"Hauptkonto";"03.01.2025";"";"GOOGLE*WORKSPACE EXAMPLE";"6837";"0,00";"29,90";"";""\n'
           '"6837";"Lizenzen";"03.01.2025";"";"GOOGLE*WORKSPACE EXAMPLE";"1813";"25,13";"0,00";"19,00";"1406"\n'
           '"1813";"Hauptkonto";"03.02.2025";"";"GOOGLE*WORKSPACE EXAMPLE";"6837";"0,00";"29,90";"";""\n'
           '"6837";"Lizenzen";"03.02.2025";"";"GOOGLE*WORKSPACE EXAMPLE";"1813";"25,13";"0,00";"19,00";"1406"\n'
           '"1813";"Hauptkonto";"04.02.2025";"";"Cafe Einmalig Rechnung";"6640";"0,00";"12,00";"";""\n')
    assert bank.learn_rules(gmbh, kon) == 2
    assert not jr.import_lines(gmbh, 2025)                                          # nur gelesen
    bank.import_datev(gmbh, DATEV)
    assert bank.apply_suggestions(gmbh, 2026) == 2
    t = {x["counterparty"][:5]: x for x in bank.transactions(gmbh, 2026) if x["kind"] == "konto"}
    assert (t["Haufe"]["target_account"], t["Haufe"]["tax_key"]) == ("6830", "19")
    assert (t["GOOGL"]["target_account"], t["GOOGL"]["tax_key"]) == ("6837", "rc_eu")


def test_lock_freezes_the_derived_lines(gmbh, ui):
    bank.import_datev(gmbh, DATEV)
    bank.assign(gmbh, by_name(gmbh, "Haufe")["id"], "konto", account="6830", tax_key="19")
    before = jr.balances(gmbh, 2026)
    jr.lock_year(ui, 2026, "Abschluss aufgestellt")
    assert jr.balances(gmbh, 2026) == before
    with pytest.raises(KontorError, match="gesperrt"):
        jr.book(gmbh, {"ref": "K1", "date": "2026-12-31", "text": "zu spät", "lines": {"6969": 1, "1370": -1}})


def test_positions_still_balance_with_bank_lines(gmbh, invoice):
    bank.import_datev(gmbh, DATEV)
    bank.apply_suggestions(gmbh, 2026)
    p = bilanz_guv(gmbh, 2026)["positionen"]
    assert p["aktiva"] == p["passiva"]


def test_bank_page_assigns(tmp_path):
    from fastapi.testclient import TestClient

    from kontor.api.app import create_app
    from kontor.context import open_ctx
    ctx = open_ctx("cli", tmp_path / "d")
    md.set_profile(ctx, {"gewinnermittlung": "bilanz", "bankimport": "datev"})
    bank.add_account(ctx, HAUPT, "Hauptkonto", "1813")
    bank.add_account(ctx, STEUER, "Steuerkonto", "1809")
    bank.import_datev(ctx, DATEV)
    tid = by_name(ctx, "Haufe")["id"]
    ctx.conn.close()
    with TestClient(create_app(tmp_path / "d"), base_url="http://127.0.0.1/m/lokal") as c:
        assert "Haufe Service Center" in c.get("/bank?jahr=2026").text
        r = c.post(f"/bank/{tid}/zuordnen", data={"jahr": "2026", "art": "konto", "konto": "6830", "steuer": "19"},
                   follow_redirects=False)
        assert r.status_code == 303
        assert "Haufe" not in c.get("/bank?jahr=2026").text                 # nur offene


def test_one_payment_settles_two_invoices(gmbh, ui, invoice):
    second = fin.finalize(ui, docs.save_draft(ui, {"kind": "rechnung", "customer_id": invoice["customer_id"],
                                                   "tax_treatment": "drittland_b2b", "doc_date": "2026-03-02",
                                                   "service_date": "2026-03-02",
                                                   "lines": [{"title": "Zusatz", "quantity": "1", "unit_price": 50000}]}))
    two = line(HAUPT, "09.06.2026", "1000,00", "Jonas Beispiel", "Rechnungen")
    bank.import_datev(gmbh, two)
    tid = by_name(gmbh, "Jonas Beispiel")["id"]
    with pytest.raises(KontorError, match="übersteigt"):
        bank.assign(gmbh, tid, "rechnung", document_ids=[invoice["id"]])          # 1.000 auf 500 offen
    bank.assign(gmbh, tid, "rechnung", document_ids=[invoice["id"], second["id"]])
    assert {docs.get_document(gmbh, i)["status"] for i in (invoice["id"], second["id"])} == {"bezahlt"}
    bank.unassign(gmbh, tid, "Test")
    assert docs.get_document(gmbh, second["id"])["status"] == "abgeschlossen"


def test_rules_never_hit_the_wrong_direction_and_amounts_find_invoices(gmbh, invoice):
    bank.add_rule(gmbh, "BEISPIEL", "6837", "19")                               # falsche Regel für einen Eingang
    beispiel = line(HAUPT, "20.03.2026", "500,00", "Kunde Beispiel", "Monatsbeitrag")   # ohne Rechnungsnummer
    bank.import_datev(gmbh, beispiel)
    s = bank.suggest(gmbh, by_name(gmbh, "Kunde Beispiel"))
    assert s["kind"] == "rechnung" and s["document_id"] == invoice["id"]


def test_reverse_charge_nets_out_in_the_balance_sheet(gmbh):
    bank.import_datev(gmbh, DATEV)
    bank.assign(gmbh, by_name(gmbh, "GOOGLE")["id"], "konto", account="6837", tax_key="rc_eu")
    r = bilanz_guv(gmbh, 2026)
    assert r["ust_saldo"] == 0 and r["positionen"]["aktiva"] == r["positionen"]["passiva"]
    assert all(b["konto"] not in ("1407", "3837") for b in r["bestand"])
    assert jr.account_names(gmbh)["1813"] == "Hauptkonto"


def voucher(vid, day, gross, tax, rate, name="Haufe Service Center"):
    return {"id": vid, "voucherNumber": f"V-{vid}", "voucherDate": f"{day}T10:00:00.000+01:00", "contactName": name,
            "taxType": "gross", "totalGrossAmount": gross, "totalTaxAmount": tax,
            "voucherItems": [{"amount": gross, "taxAmount": tax, "taxRatePercent": rate}]}


def test_receipt_from_lexware_links_to_the_bank_row_and_flags_wrong_vat(gmbh):
    from kontor.importers.lexware import import_purchase_voucher
    bank.import_datev(gmbh, DATEV)
    bank.assign(gmbh, by_name(gmbh, "Haufe")["id"], "konto", account="6837", tax_key="19")
    bank.assign(gmbh, by_name(gmbh, "Leasing")["id"], "konto", account="6420", tax_key="0")
    assert any(f["art"] == "vorsteuer_ohne_beleg" for f in auffaelligkeiten(gmbh, 2026))
    rid = import_purchase_voucher(gmbh, voucher("a", "2026-01-06", 59.50, 9.50, 19), b"%PDF-a", "haufe.pdf")
    assert import_purchase_voucher(gmbh, voucher("a", "2026-01-06", 59.50, 9.50, 19), b"%PDF-a", "haufe.pdf") == rid
    import_purchase_voucher(gmbh, voucher("b", "2026-02-01", 119.00, 19.00, 19, "Mustermann"), b"%PDF-b", "m.pdf")
    assert bank.link_receipts(gmbh, 2026) == 2
    found = {f["art"]: f for f in auffaelligkeiten(gmbh, 2026)}
    assert "vorsteuer_ohne_beleg" not in found
    assert found["steuer_beleg_abweichend"]["betrag"] == 1900          # Beleg 19 %, Zuordnung 0: prüfen
    assert jr.balances(gmbh, 2026)["1406"] == 950


def test_ustva_for_a_balance_sheet_mandant_comes_from_the_bank(gmbh, invoice):
    from kontor.reports.ustva import ustva
    bank.import_datev(gmbh, DATEV)
    bank.apply_suggestions(gmbh, 2026)                                           # Rechnung Drittland
    bank.assign(gmbh, by_name(gmbh, "Haufe")["id"], "konto", account="6837", tax_key="19")
    bank.assign(gmbh, by_name(gmbh, "GOOGLE")["id"], "konto", account="6837", tax_key="rc_eu")
    u = ustva(gmbh, "2026")["fields"]
    assert u["66"]["tax"] == 950 and u["46"]["basis"] == 29 and u["47"]["tax"] == 568 and u["67"]["tax"] == 568
    assert u["45"]["basis"] == 500 and u["83"]["tax"] == -950
