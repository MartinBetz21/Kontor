"""Lohn (Profilschalter lohn, SPEC.md „Payroll“). Erfundene Mitarbeiter und Beträge. Die Rechenkerne selbst testen
test_lohnsteuer.py (gegen den BMF) und test_sozialversicherung.py; hier geht es um das Zusammenspiel: Merkmale je
Monat, Bezüge, Netto, Festschreiben, Reihenfolge, Storno, Buchungen, Anmeldung und Bescheinigung."""
import json
import os
from datetime import date
from decimal import Decimal

import pytest

from kontor.core import lohnsteuer as lst
from kontor.core import sozialversicherung as sv
from kontor.core.errors import HumanOnly, KontorError
from kontor.services import journal as jr
from kontor.services import lohn
from kontor.services import masterdata as md

SALDEN = """Konto;Bezeichnung;Saldo;S/H
1800;Bank;5000,00;S
2900;Gezeichnetes Kapital;5000,00;H
"""


@pytest.fixture
def firma(ctx):
    md.set_profile(ctx, {"gewinnermittlung": "bilanz", "kontenrahmen": "skr04", "lohn": "ja"})
    md.set_settings(ctx, {"name": "Muster GmbH", "street": "Musterweg 1", "postcode": "50667", "city": "Köln",
                          "tax_number": "214/0000/0000", "share_capital": "25000,00"})
    jr.opening_from_saldenliste(ctx, 2026, SALDEN)
    return ctx


def gf(ctx, **merkmale) -> int:
    """Gesellschafter-Geschäftsführer ohne Sozialversicherung mit symbolischem Gehalt."""
    mid = lohn.mitarbeiter_speichern(ctx, {"personalnr": "1", "vorname": "Erika", "nachname": "Muster",
                                           "geburtsdatum": "1980-05-10", "eintritt": "2024-01-01",
                                           "steuer_id": "12345678901", "strasse": "Weg 2", "plz": "50667",
                                           "ort": "Köln"})
    lohn.merkmale_setzen(ctx, mid, "2024-01", {"steuerklasse": 5, "personengruppe": "", "beitragsgruppe": "0000",
                                               "aufwandskonto": "6024", **merkmale})
    lohn.bezug(ctx, mid, {"lohnart": "gehalt", "betrag": "1,00", "ab": "2024-01"})
    return mid


def angestellt(ctx, **merkmale) -> int:
    mid = lohn.mitarbeiter_speichern(ctx, {"personalnr": "2", "vorname": "Max", "nachname": "Beispiel",
                                           "geburtsdatum": "1990-03-01", "eintritt": "2026-01-01",
                                           "steuer_id": "98765432109", "strasse": "Gasse 5", "plz": "50667",
                                           "ort": "Köln"})
    lohn.merkmale_setzen(ctx, mid, "2026-01", {"steuerklasse": 1, "konfession": "ev", "krankenkasse": "Muster-BKK",
                                               "kv_zusatz": "2,5", "u1": "1,6", "u2": "0,44", **merkmale})
    lohn.bezug(ctx, mid, {"lohnart": "gehalt", "bezeichnung": "Gehalt", "betrag": "3000,00", "ab": "2026-01"})
    return mid


def test_switch_off_means_no_payroll(ctx):
    with pytest.raises(KontorError, match="Profilschalter lohn"):
        lohn.mitarbeiter_speichern(ctx, {"personalnr": "1", "vorname": "A", "nachname": "B", "eintritt": "2026-01-01"})
    assert lohn.buchungen(ctx, 2026) == []


def test_symbolic_salary_without_social_insurance(firma, ui):
    mid = gf(firma)
    d = lohn.berechnen(firma, mid, "2026-03")
    s = d["summen"]
    assert (s["brutto"], s["lst"], s["soli"], s["kist"], s["sv_an"], s["sv_ag"]) == (100, 0, 0, 0, 0, 0)
    assert s["netto"] == s["auszahlung"] == s["ag_kosten"] == 100
    assert d["sv"]["art"] == "keine" and s["sv_tage"] == 0
    assert d["pap"]["eingaben"]["STKL"] == "5" and d["pap"]["eingaben"]["KRV"] == "1"
    assert d["fehlende_vormonate"] == ["2026-01", "2026-02"]


def test_salary_with_church_tax_matches_the_cores(firma):
    mid = angestellt(firma)
    d = lohn.berechnen(firma, mid, "2026-01")
    s = d["summen"]
    pap = lst.lohnsteuer(2026, **{k: Decimal(v) if "." in v else int(v) for k, v in d["pap"]["eingaben"].items()})
    assert s["lst"] == pap["lst"] > 0 and s["soli"] == pap["soli"]
    assert s["kist"] == int(Decimal(pap["kist_basis"]) * 9 / 100)          # 9 %, auf den Cent abgerundet
    beitraege = sv.beitraege(2026, entgelt=300000, beitragsgruppe="1111", kv_zusatz=Decimal("2.5"),
                             pv_kinderlos=True, u1=Decimal("1.6"), u2=Decimal("0.44"))
    for k in ("kv_an", "rv_an", "av_an", "pv_an", "kv_ag", "rv_ag", "av_ag", "pv_ag", "u1", "u2", "insolvenz"):
        assert s[k] == beitraege[k], k
    assert d["kinderlos"] and s["pv_an"] > s["pv_ag"]                      # Kinderlosenzuschlag trägt der AN
    assert s["netto"] == 300000 - s["lst"] - s["soli"] - s["kist"] - s["sv_an"] == s["auszahlung"]
    assert s["ag_kosten"] == 300000 + s["sv_ag"] + s["u1"] + s["u2"] + s["insolvenz"]


def test_merkmale_apply_from_their_month(firma):
    mid = angestellt(firma)
    lohn.merkmale_setzen(firma, mid, "2026-04", {"steuerklasse": 3})
    assert lohn.berechnen(firma, mid, "2026-03")["merkmale"]["steuerklasse"] == 1
    april = lohn.berechnen(firma, mid, "2026-04")
    assert april["merkmale"]["steuerklasse"] == 3 and april["merkmale"]["konfession"] == "ev"   # Rest übernommen
    assert april["summen"]["lst"] < lohn.berechnen(firma, mid, "2026-03")["summen"]["lst"]
    with pytest.raises(KontorError, match="Steuerklasse"):
        lohn.merkmale_setzen(firma, mid, "2026-05", {"steuerklasse": 7})
    with pytest.raises(KontorError, match="Beitragsgruppe"):
        lohn.merkmale_setzen(firma, mid, "2026-05", {"beitragsgruppe": "11"})


def test_finalize_in_order_logged_with_actor_and_immutable(firma, ui):
    mid = angestellt(firma)
    with pytest.raises(KontorError, match="Vormonate"):
        lohn.festschreiben(ui, mid, "2026-02")
    jan = lohn.festschreiben(firma, mid, "2026-01")                         # der Agent darf festschreiben
    assert firma.conn.execute("SELECT actor FROM audit_log WHERE action = 'lohn_festschreiben'").fetchone()[0] == "cli"
    a = lohn.abrechnung(ui, jan)
    assert a["status"] == "fest" and a["pdf"].startswith("archiv/lohn/2026/")
    pdf = ui.data / a["pdf"]
    assert pdf.read_bytes()[:5] == b"%PDF-" and not os.access(pdf, os.W_OK)
    with pytest.raises(Exception, match="ändert sich nicht"):
        with ui.conn:
            ui.conn.execute("UPDATE lohn_abrechnungen SET brutto = 1 WHERE id = ?", (jan,))
    with pytest.raises(KontorError, match="festgeschriebene"):
        lohn.bezug(firma, mid, {"lohnart": "einmalzahlung", "betrag": "100,00", "monat": "2026-01"})
    feb = lohn.festschreiben(ui, mid, "2026-02")
    with pytest.raises(KontorError, match="späteren Monate"):
        lohn.stornieren(ui, jan, "falsche Steuerklasse")
    with pytest.raises(KontorError, match="Grund"):
        lohn.stornieren(ui, feb, " ")
    lohn.stornieren(ui, feb, "falscher Zusatzbeitrag")
    assert lohn.abrechnung(ui, feb)["status"] == "storniert"
    assert lohn.festschreiben(ui, mid, "2026-02") != feb                   # Monat neu festschreiben geht


def test_one_off_payment_is_taxed_as_other_income(firma, ui):
    mid = angestellt(firma)
    lohn.festschreiben(ui, mid, "2026-01")
    lohn.bezug(firma, mid, {"lohnart": "einmalzahlung", "bezeichnung": "Prämie", "betrag": "1000,00",
                            "monat": "2026-02"})
    d = lohn.berechnen(firma, mid, "2026-02")
    jan = lohn.abrechnung(ui, lohn.festgeschrieben(ui, 2026, mid)[0]["id"])["summen"]
    assert d["pap"]["eingaben"]["SONSTB"] == "100000"
    assert d["pap"]["eingaben"]["JRE4"] == str(300000 + 300000 * 11)        # bisher + laufend × übrige Monate
    assert d["summen"]["lst"] == d["pap"]["ergebnis"]["lst"] + d["pap"]["ergebnis"]["lst_sonst"] > jan["lst"]
    assert d["summen"]["steuerbrutto_einmal"] == 100000 and d["summen"]["brutto"] == 400000


def test_benefits_in_kind_tax_free_and_deductions(firma, ui):
    mid = angestellt(firma)
    lohn.bezug(firma, mid, {"lohnart": "sachbezug", "bezeichnung": "Dienstwagen", "betrag": "400,00", "ab": "2026-01"})
    lohn.bezug(firma, mid, {"lohnart": "steuerfrei", "bezeichnung": "Auslagenersatz", "betrag": "50,00",
                            "monat": "2026-01"})
    lohn.bezug(firma, mid, {"lohnart": "abzug", "bezeichnung": "Vorschuss", "betrag": "200,00", "monat": "2026-01"})
    s = lohn.berechnen(firma, mid, "2026-01")["summen"]
    assert s["steuerbrutto"] == 340000 and s["brutto"] == 345000
    assert s["auszahlung"] == s["netto"] - 40000 - 20000
    lohn.festschreiben(ui, mid, "2026-01")
    zeilen = lohn.buchungen(ui, 2026)
    by = {}
    for z in zeilen:
        by[z["account"]] = by.get(z["account"], 0) + z["amount"]
    assert sum(by.values()) == 0
    assert by["6020"] == 340000 and by["6130"] == 5000 and by["4947"] == -40000 and by["1340"] == -20000
    assert by["3720"] == -s["auszahlung"] and by["3730"] == -(s["lst"] + s["soli"] + s["kist"])
    assert by["3740"] == -(s["sv_an"] + s["sv_ag"] + s["u1"] + s["u2"] + s["insolvenz"])
    assert by["6110"] == s["sv_ag"] + s["u1"] + s["u2"] + s["insolvenz"]


def test_bookings_reach_the_journal_on_the_last_day_of_the_month(firma, ui):
    mid = gf(firma)
    lohn.vortrag_setzen(firma, mid, 2026, "2026-01", {"steuerbrutto": "1,00", "brutto": "1,00"})
    with pytest.raises(KontorError, match="Unbekannte"):
        lohn.vortrag_setzen(firma, mid, 2026, "2026-01", {"irgendwas": "1,00"})
    assert lohn.berechnen(firma, mid, "2026-02")["fehlende_vormonate"] == []
    lohn.festschreiben(ui, mid, "2026-02")
    rows = [r for r in jr.lines(ui, 2026) if r["ref"].startswith("LOHN")]
    assert {(r["date"], r["account"], r["amount"]) for r in rows} == {("2026-02-28", "6024", 100),
                                                                      ("2026-02-28", "3720", -100)}
    k = lohn.lohnkonto(ui, mid, 2026)
    assert k["summe"]["steuerbrutto"] == 200 and k["vortrag"]["bis"] == "2026-01"


def test_minijob_with_flat_tax_stays_out_of_the_tax_return(firma, ui):
    mid = lohn.mitarbeiter_speichern(firma, {"personalnr": "3", "vorname": "Lea", "nachname": "Aushilfe",
                                             "geburtsdatum": "2000-01-01", "eintritt": "2026-01-01"})
    lohn.merkmale_setzen(firma, mid, "2026-01", {"pauschsteuer": "ja", "personengruppe": "109", "beitragsgruppe": "6500",
                                                 "krankenkasse": "Muster-BKK", "rv_aufstockung": "nein"})
    lohn.bezug(firma, mid, {"lohnart": "gehalt", "betrag": "500,00", "ab": "2026-01"})
    s = lohn.berechnen(firma, mid, "2026-01")["summen"]
    assert s["lst"] == 0 and s["pauschsteuer"] == 1000 and s["sv_an"] == 0 and s["auszahlung"] == 50000
    assert s["kv_pauschal"] == 6500 and s["rv_pauschal"] == 7500
    assert s["u1"] == 400 and s["u2"] == 110                                # Sätze der Minijob-Zentrale 2026
    lohn.festschreiben(ui, mid, "2026-01")
    kassen = lohn.beitraege_kassen(ui, "2026-01")
    assert [k["kasse"] for k in kassen] == ["Minijob-Zentrale"] and kassen[0]["pauschsteuer"] == 1000
    assert lohn.lsta_werte(ui, 2026, "01")["kz"]["arbeitnehmer"] == 0
    with pytest.raises(KontorError, match="Pauschsteuer"):
        lohn.lstb_werte(ui, mid, 2026)


def test_tax_return_values_and_periods(firma, ui):
    mid = angestellt(firma)
    gf_id = gf(firma)
    for m in ("2026-01", "2026-02", "2026-03"):
        lohn.festschreiben(ui, gf_id, m)
        lohn.festschreiben(ui, mid, m)
    w = lohn.lsta_werte(ui, 2026, "02")
    s = lohn.abrechnung(ui, lohn.festgeschrieben(ui, 2026, mid, ["2026-02"])[0]["id"])["summen"]
    assert w["kz"]["arbeitnehmer"] == 2 and w["kz"]["lst"] == s["lst"] and w["kz"]["kist"] == {"ev": s["kist"]}
    assert w["kz"]["gesamt"] == s["lst"] + s["soli"] + s["kist"]
    md.set_settings(firma, {"lohn_anmeldezeitraum": "quartal"})
    q = lohn.zeitraeume(ui, 2026)
    assert [z["zeitraum"] for z in q] == ["41", "42", "43", "44"] and q[0]["kz"]["lst"] == 3 * s["lst"]
    assert q[0]["faellig"] == "2026-04-10" and q[1]["offen"]
    satz, _ = lohn.lsta_satz(ui, 2026, "41")
    assert satz.kz42 == 3 * s["lst"] and satz.kz61 == 3 * s["kist"] and satz.arbeitnehmer == 2 and not satz.berichtigt
    with pytest.raises(KontorError, match="nicht alles festgeschrieben"):
        lohn.lsta_satz(ui, 2026, "42")


def test_wage_tax_certificate_periods_and_lines(firma, ui):
    mid = angestellt(firma)
    lohn.merkmale_setzen(firma, mid, "2026-03", {"kinderfreibetraege": "1"})
    for m in ("2026-01", "2026-02", "2026-03"):
        lohn.festschreiben(ui, mid, m)
    lohn.mitarbeiter_speichern(firma, {"austritt": "2026-03-31"}, mid)
    b = lohn.lstb_werte(ui, mid, 2026)
    assert (b["von"], b["bis"]) == ("2026-01-01", "2026-03-31")
    assert [(p["von"], p["bis"], p["kinderfreibetraege"]) for p in b["merkmale"]] == \
        [("2026-01-01", "2026-02-28", "0"), ("2026-03-01", "2026-03-31", "1")]
    k = lohn.lohnkonto(ui, mid, 2026)["summe"]
    assert b["zeile3_brutto"] == 900000 and b["zeile4_lst"] == k["lst"] and b["zeile23a_rv_an"] == k["rv_an"]
    assert b["zeile22a_rv_ag"] == k["rv_ag"] and b["zeile25_kv_an"] == k["kv_an"] and b["zeile27_av_an"] == k["av_an"]


def test_findings_name_missing_months_and_due_returns(firma, ui):
    mid = angestellt(firma)
    firma.today = ui.today = date(2026, 3, 5)
    texte = " ".join(lohn.auffaelligkeiten(ui, 2026))
    assert "2026-01 ist nicht festgeschrieben" in texte and "2026-02 ist nicht festgeschrieben" in texte
    assert "Lohnsteuer-Anmeldung Februar 2026 fällig am 10.03.2026" in texte


def test_reports_csv_and_pdf(firma, ui):
    from kontor.render.lohn import lohnkonto
    mid = angestellt(firma)
    lohn.festschreiben(ui, mid, "2026-01")
    csv = lohn.lohnjournal_csv(ui, 2026).splitlines()
    assert csv[0].startswith("monat;personalnr;name;brutto") and csv[1].startswith("2026-01;2;Max Beispiel;3000,00")
    assert lohnkonto(lohn.lohnkonto(ui, mid, 2026))[:5] == b"%PDF-"
    kosten = lohn.lohnkosten(ui, 2026)
    assert kosten["summe"]["ag_kosten"] == lohn.abrechnung(ui, lohn.festgeschrieben(ui, 2026)[0]["id"])["summen"]["ag_kosten"]
    assert json.loads(json.dumps(lohn.lohnjournal(ui, 2026), default=str))["summe"]["brutto"] == 300000


def test_pages_only_with_switch(tmp_path):
    from fastapi.testclient import TestClient

    from kontor.api.app import create_app
    from kontor.context import open_ctx
    ctx = open_ctx("cli", tmp_path / "d")
    with TestClient(create_app(tmp_path / "d"), base_url="http://127.0.0.1/m/lokal") as c:
        assert "/lohn" not in c.get("/").text and c.get("/lohn").status_code == 404
    md.set_profile(ctx, {"gewinnermittlung": "bilanz", "lohn": "ja"})
    md.set_settings(ctx, {"name": "Muster GmbH", "street": "Musterweg 1", "postcode": "50667", "city": "Köln"})
    mid = angestellt(ctx)
    ctx.conn.close()
    app = create_app(tmp_path / "d")
    with TestClient(app, base_url="http://127.0.0.1/m/lokal") as c:
        assert "Max Beispiel" in c.get("/lohn?monat=2026-01").text
        assert "Merkmale ändern" in c.get(f"/lohn/mitarbeiter/{mid}").text
        assert "Rechenweg" in c.get(f"/lohn/vorschau/{mid}/2026-01").text
        assert "lohn_konto_gehalt" in c.get("/einstellungen").text
        assert c.post("/lohn/festschreiben", data={"mitarbeiter": mid, "monat": "2026-01"}).status_code == 403
        r = c.post("/lohn/festschreiben", data={"mitarbeiter": mid, "monat": "2026-01",
                                                "confirm": app.state.confirm["lokal"]}, follow_redirects=False)
        assert r.status_code == 303
        page = c.get("/lohn?monat=2026-01").text
        assert "festgeschrieben" in page
        aid = int(page.split("/lohn/abrechnung/")[1].split('"')[0])
        assert c.get(f"/lohn/abrechnung/{aid}/pdf").content[:5] == b"%PDF-"
        assert "Lohnsteuer-Anmeldungen" in c.get("/lohn/jahr?jahr=2026").text
        assert c.get(f"/lohn/lohnkonto/{mid}/2026.pdf").content[:5] == b"%PDF-"
        assert c.get(f"/lohn/lohnzettel/{mid}/2026.pdf").content[:5] == b"%PDF-"
        assert c.get("/export/lohnjournal/2026.csv").text.startswith("monat;")


def test_factor_and_private_insurance_reach_the_pap(firma):
    mid = angestellt(firma, steuerklasse=4, faktor="0,9", beitragsgruppe="0110", pkv_beitrag="400,00",
                     pkv_pv_beitrag="50,00")
    d = lohn.berechnen(firma, mid, "2026-01")
    e = d["pap"]["eingaben"]
    assert e["af"] == "1" and e["f"] == "0.900" and e["PKV"] == "1" and e["PKPV"] == "45000"
    assert int(e["PKPVAGZ"]) == d["summen"]["pkv_zuschuss"] + d["summen"]["pv_zuschuss"] > 0
    ohne = lohn.berechnen(firma, angestellt_zwei(firma), "2026-01")["summen"]["lst"]
    assert d["summen"]["lst"] < ohne                                        # Faktor < 1 mindert die Lohnsteuer


def angestellt_zwei(ctx) -> int:
    mid = lohn.mitarbeiter_speichern(ctx, {"personalnr": "9", "vorname": "Ida", "nachname": "Vergleich",
                                           "geburtsdatum": "1990-03-01", "eintritt": "2026-01-01"})
    lohn.merkmale_setzen(ctx, mid, "2026-01", {"steuerklasse": 4, "konfession": "ev", "beitragsgruppe": "0110",
                                               "pkv_beitrag": "400,00", "pkv_pv_beitrag": "50,00"})
    lohn.bezug(ctx, mid, {"lohnart": "gehalt", "betrag": "3000,00", "ab": "2026-01"})
    return mid


# -- ELStAM ---------------------------------------------------------------------------------------------------------
ERIC = os.environ.get("KONTOR_ERIC", "")


def eintrag(**f) -> dict:
    """Ein Eintrag, wie elster.elstam.aenderungsliste_lesen ihn liefert."""
    return {"idnr": "98765432109", "art": "Änderung", "gueltig_ab": date(2026, 4, 1), "steuerklasse": 3, "faktor": "",
            "kinderfreibetraege": "1.0", "konfession": "ev", "konfession_ehegatte": "", "freibetrag": 0,
            "hinzurechnung": 0, "freibetrag_jahr": 0, "hinzurechnung_jahr": 0, **f}


def test_elstam_are_stored_compared_and_taken_over(firma, ui):
    mid = angestellt(firma)
    assert lohn.elstam_speichern(firma, None, [eintrag(), eintrag()]) == 1            # doppelt geliefert zählt einmal
    e = lohn.elstam(firma, offen=True)[0]
    assert e["mitarbeiter_id"] == mid and e["merkmale"]["kinderfreibetraege"] == "1"
    assert e["abweichung"] == {"steuerklasse": 3, "kinderfreibetraege": "1"}
    assert any("ELStAM für Max Beispiel ab 2026-04 weichen ab" in f for f in lohn.auffaelligkeiten(firma, 2026))
    lohn.elstam_uebernehmen(firma, e["id"])
    assert lohn.merkmale_im(firma, mid, "2026-03")["steuerklasse"] == 1
    april = lohn.merkmale_im(firma, mid, "2026-04")
    assert (april["steuerklasse"], april["kinderfreibetraege"], april["krankenkasse"]) == (3, "1", "Muster-BKK")
    assert lohn.elstam(firma, offen=True) == [] and not any("ELStAM" in f for f in lohn.auffaelligkeiten(firma, 2026))


def test_elstam_for_unknown_ids_and_rejections(firma):
    angestellt(firma)
    lohn.elstam_speichern(firma, None, [eintrag(idnr="11111111111"),
                                        eintrag(art="Ablehnung", gueltig_ab=None, steuerklasse=None, text="unbekannt")])
    fremd, ablehnung = sorted(lohn.elstam(firma), key=lambda e: e["art"])[::-1]
    with pytest.raises(KontorError, match="keinen Mitarbeiter"):
        lohn.elstam_uebernehmen(firma, fremd["id"])
    assert any("ELStAM-Ablehnung für Max Beispiel: unbekannt" in f for f in lohn.auffaelligkeiten(firma, 2026))
    lohn.elstam_verwerfen(firma, fremd["id"], "nicht bei uns beschäftigt")
    assert len(lohn.elstam(firma, offen=True)) == 1


def test_elstam_sending_and_fetching_are_human_only(firma):
    mid = angestellt(firma)
    with pytest.raises(HumanOnly):
        lohn.elstam_senden(firma, "anmelden", [mid], "0000", True)
    with pytest.raises(HumanOnly):
        lohn.elstam_abholen(firma, "0000", True)
    with pytest.raises(KontorError, match="ELStAM-Vorgang"):
        lohn._elstam_an(firma, "irgendwas", [mid], None)
    with pytest.raises(KontorError, match="Austritt"):
        lohn._elstam_an(firma, "abmelden", [mid], None)


@pytest.mark.skipif(not ERIC, reason="KONTOR_ERIC nicht gesetzt")
def test_elstam_requests_pass_eric(firma):
    md.set_settings(firma, {"tax_number": "198/113/10134", "finanzamt_nr": "9198", "eric_pfad": ERIC})
    mid = angestellt(firma)
    lohn.mitarbeiter_speichern(firma, {"steuer_id": "01127645382"}, mid)    # Test-IdNr (beginnt mit 0, nur mit Testmerker)
    for art in ("anmelden", "wechsel"):
        r = lohn.elstam_pruefen(firma, art, [mid], "2026-10-01")
        assert r["ok"], r
    assert "01127645382" in lohn.elstam_satz(firma, "anmelden", [mid], None, True)
    with pytest.raises(KontorError, match="Referenzdatum"):
        lohn.elstam_satz(firma, "wechsel", [mid], None, True)
    lohn.mitarbeiter_speichern(firma, {"austritt": "2026-10-31"}, mid)
    assert lohn.elstam_pruefen(firma, "abmelden", [mid], "2026-01-01")["ok"]


def test_certificate_periods_cover_carried_over_months(firma, ui):
    mid = gf(firma)
    lohn.vortrag_setzen(firma, mid, 2026, "2026-09", {"steuerbrutto": "9,00", "brutto": "9,00"})
    for m in ("2026-10", "2026-11", "2026-12"):
        lohn.festschreiben(ui, mid, m)
    b = lohn.lstb_werte(ui, mid, 2026)
    assert [(p["von"], p["bis"], p["steuerklasse"]) for p in b["merkmale"]] == [("2026-01-01", "2026-12-31", 5)]
    assert b["zeile3_brutto"] == 1200


def test_eric_hints_only_count_as_plausible():
    r = lohn._plausibel({"ok": False, "code": 610001003, "text": "Hinweise …", "meldungen": ["Zeitraum"]})
    assert r["ok"] and r["meldungen"] == ["Zeitraum"]
    assert not lohn._plausibel({"ok": False, "code": 610001002, "text": "Fehler", "meldungen": []})["ok"]


def test_returns_sent_by_another_program_are_not_sent_again(firma, ui):
    mid = gf(firma)
    firma.today = ui.today = date(2026, 11, 2)
    for m in ("2026-01", "2026-02"):
        lohn.festschreiben(ui, mid, m)
    assert any("Januar 2026" in f and "noch nicht gesendet" in f for f in lohn.auffaelligkeiten(ui, 2026))
    with pytest.raises(KontorError, match="Notiz"):
        lohn.lsta_extern(firma, 2026, "01", "")
    lohn.lsta_extern(firma, 2026, "01", "Lexware, Februar 2026")
    texte = " ".join(lohn.auffaelligkeiten(ui, 2026))
    assert "Januar 2026" not in texte and "Februar 2026 fällig" in texte
    with pytest.raises(KontorError, match="anderes Programm"):
        lohn.lsta_satz(ui, 2026, "01")


def test_bulk_finalize_and_external_returns_before_finalizing(firma, ui):
    mid = gf(firma)
    firma.today = ui.today = date(2026, 10, 22)
    for z in ("01", "02", "03"):
        lohn.lsta_extern(firma, 2026, z, "Lexware")                       # vor dem Festschreiben, Werte aus Entwürfen
    assert len(lohn.festschreiben_bis(firma, mid, "2026-09")) == 9
    z = {x["zeitraum"]: x for x in lohn.zeitraeume(ui, 2026)}
    assert z["01"]["gesendet"] and not z["01"]["abweichung"] and z["01"]["kz"]["arbeitnehmer"] == 1
    texte = " ".join(lohn.auffaelligkeiten(ui, 2026))
    assert "nicht festgeschrieben" not in texte and "Januar 2026" not in texte and "April 2026 fällig" in texte


def test_periods_show_draft_values_so_external_returns_do_not_differ(firma, ui):
    gf(firma)
    firma.today = ui.today = date(2026, 10, 22)
    lohn.lsta_extern(firma, 2026, "01", "Lexware")
    z = lohn.zeitraeume(ui, 2026)[0]
    assert z["kz"]["arbeitnehmer"] == 1 and z["offen"] and not z["abweichung"]
    assert not any("weichen von der gesendeten ab" in f for f in lohn.auffaelligkeiten(ui, 2026))
