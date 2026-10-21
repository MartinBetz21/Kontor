from kontor.reports.euer import euer
from kontor.reports.export import journal_csv
from kontor.reports.overview import overview
from kontor.reports.ustva import jahresuebersicht, ustva
from kontor.services import masterdata as md
from kontor.services import payments as pay
from kontor.services import receipts as rc
from tests.test_payments import invoice  # noqa: F401


def receipt(ctx, ui, name, **fields):
    rid = rc.add_file(ctx, f"beleg {name}".encode(), f"{name}.jpg", "upload")["id"]
    cat = next(c["id"] for c in md.list_categories(ctx) if c["name"] == "Software und Abos")
    rc.update(ctx, rid, {"category_id": cat, "supplier": name, **fields})
    return rc.confirm(ui, rid)


def scenario(ctx, ui, invoice):
    small = invoice(25126)                                    # 299,00 brutto
    pay.record_payment(ctx, small, "2026-10-05", 29900)
    receipt(ctx, ui, "Anthropic", receipt_date="2026-10-01", gross=10000, tax_kind="rc_eu", paid_on="2026-10-01")
    receipt(ctx, ui, "Hetzner", receipt_date="2026-10-03", gross=11900, tax_kind="inland_19", paid_on="2026-10-03")
    receipt(ctx, ui, "Altbeleg", receipt_date="2026-09-20", gross=1190, tax_kind="inland_19", paid_on="2026-11-02")


def test_ustva_october(ctx, ui, invoice):
    scenario(ctx, ui, invoice)
    r = ustva(ctx, "2026-10")
    f = r["fields"]
    assert (f["81"]["basis"], f["81"]["tax"]) == (251, 4769)          # ELSTER rechnet aus vollen Euro
    assert r["exact"]["19"] == {"net": 25126, "tax": 4774}
    assert (f["46"]["basis"], f["47"]["tax"], f["67"]["tax"]) == (100, 1900, 1900)
    assert f["66"]["tax"] == 1900                                      # nur Hetzner; Altbeleg erst im November bezahlt
    assert r["zahllast"] == 4769 + 1900 - 1900 - 1900
    assert r["deadline"] == "2026-11-10"
    assert len(r["details"]["einnahmen"]) == 1 and len(r["details"]["reverse_charge"]) == 1


def test_ustva_quarter_and_november(ctx, ui, invoice):
    scenario(ctx, ui, invoice)
    assert ustva(ctx, "2026-11")["fields"]["66"]["tax"] == 0           # Vorsteuer nach Rechnungsdatum
    assert ustva(ctx, "2026-Q4")["fields"]["66"]["tax"] == 1900
    assert ustva(ctx, "2026-Q3")["fields"]["66"]["tax"] == 190


def test_euer(ctx, ui, invoice):
    scenario(ctx, ui, invoice)
    pay.record_tax_payment(ctx, "2026-11-10", -869, "UStVA 10")
    e = euer(ctx, 2026)
    assert (e["einnahmen_netto"], e["ust_vereinnahmt"]) == (25126, 4774)
    assert e["ausgaben_netto"] == 10000 + 10000 + 1000
    assert (e["vorsteuer"], e["ust_an_fa"]) == (1900 + 190, 869)
    assert e["gewinn"] == 25126 + 4774 - 21000 - 2090 - 869


def test_overview(ctx, ui, invoice):
    scenario(ctx, ui, invoice)
    open_id = invoice(100000)
    o = overview(ctx)
    assert [i["id"] for i in o["open_invoices"]] == [open_id]
    oct_ = next(m for m in o["months"] if m["month"] == "2026-10")
    assert (oct_["in"], oct_["out"]) == (29900, 10000 + 11900)
    assert o["period"] == "2026-10" and o["inbox"] == 0


def test_journal_csv(ctx, ui, invoice):
    scenario(ctx, ui, invoice)
    csv = journal_csv(ctx, 2026)
    lines = csv.strip().splitlines()
    assert lines[0].startswith("Datum;Art;Beleg;Partner;Netto;Steuer;Brutto")
    assert any(";Einnahme;RE-2026-001;" in l and "251,26" in l for l in lines)
    assert any("Anthropic" in l and "rc_eu" in l for l in lines)


def test_vorsteuer_follows_invoice_date_not_payment(ctx, ui, invoice):
    scenario(ctx, ui, invoice)          # Altbeleg: Rechnung 20.09., bezahlt 02.11.
    assert ustva(ctx, "2026-09")["fields"]["66"]["tax"] == 190
    assert ustva(ctx, "2026-11")["fields"]["66"]["tax"] == 0


def test_jahresuebersicht_umsatzsteuer(ctx, ui, invoice):
    scenario(ctx, ui, invoice)
    pay.record_tax_payment(ctx, "2026-11-10", -869, "UStVA 10")
    j = jahresuebersicht(ctx, 2026)
    assert j["einbehalten"] == 4774                     # vereinnahmt, ohne Abzug der Vorsteuer
    assert j["vorsteuer"] == 1900 + 190                 # Hetzner Oktober, Altbeleg September
    assert j["zahllast"] == 4774 - 2090
    assert j["gezahlt"] == 869
    assert j["offen"] == 4774 - 2090 - 869
    assert j["reverse_charge"] == 1900                  # nur Hinweis, zahlungsneutral
    assert [r["period"] for r in j["zeitraeume"]][:2] == ["2026-01", "2026-02"]
    okt = next(r for r in j["zeitraeume"] if r["period"] == "2026-10")
    assert (okt["einbehalten"], okt["vorsteuer"], okt["zahllast"]) == (4774, 1900, 2874)
    assert sum(r["zahllast"] for r in j["zeitraeume"]) == j["zahllast"]

