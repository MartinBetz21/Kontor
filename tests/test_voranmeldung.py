"""Umsatzsteuer-Voranmeldung als Auftrag zur Freigabe (Profilschalter ustva_eric). ELSTER wird nie erreicht: ERiC ist
hier durch Stellvertreter ersetzt, die nur antworten; die echte lokale Prüfung des Datensatzes steht in
test_elster_ustva.py."""
from datetime import date

import pytest

from kontor.core.errors import HumanOnly, KontorError
from kontor.elster import eric
from kontor.services import auftraege
from kontor.services import masterdata as md
from kontor.services import payments as pay
from kontor.services import receipts as rc
from kontor.services import voranmeldung as va
from tests.test_payments import invoice  # noqa: F401
from tests.test_reports import receipt


@pytest.fixture
def september(ctx, ui, invoice):
    ctx.today = ui.today = date(2026, 11, 2)
    md.set_settings(ctx, {"ustva_period": "monat", "finanzamt_nr": "9198"})
    pay.record_payment(ctx, invoice(10000), "2026-09-15", 11900)          # 119,00 brutto
    receipt(ctx, ui, "Community", receipt_date="2026-09-18", gross=990, tax_kind="rc_drittland", paid_on="2026-09-18")
    return ctx


@pytest.fixture
def eric_antwortet(monkeypatch):
    """ERiC-Stellvertreter: prüft immer plausibel, sendet ohne Netz und merkt sich die Datensätze."""
    gesendet = []
    monkeypatch.setattr(eric, "steuernummer13", lambda st, bufa: "9198011310134")
    monkeypatch.setattr(eric, "pruefen", lambda xml, art: {"ok": False, "code": 610001003, "text": "Hinweise",
                                                           "meldungen": [], "rueckgabe": "", "serverantwort": ""})

    def senden(xml, art, zertifikat, pin, protokoll):
        gesendet.append((art, pin, xml))
        return {"ok": True, "code": 0, "text": "Verarbeitung fehlerfrei.", "meldungen": [], "transferticket": "TT1",
                "protokoll": ""}
    monkeypatch.setattr(eric, "senden", senden)
    return gesendet


def test_werte_kommen_aus_der_umsatzsteuer_seite(september):
    w = va.werte(september, "2026-09")
    assert w["kz"] == {"81": 100, "84": 9, "85": 188, "67": 188}
    assert (w["kz83"], w["berichtigt"], w["faellig"]) == (1900, False, "2026-10-12")
    assert va.name("2026-09") == "September 2026" and va.name("2026-Q3") == "3. Quartal 2026"


def test_keine_voranmeldung_ohne_grundlage(september, ui):
    with pytest.raises(KontorError, match="noch nicht vorbei"):
        va.werte(september, "2026-11")
    rid = rc.add_file(september, b"offen", "offen.jpg", "upload")["id"]
    rc.update(september, rid, {"supplier": "Hosting", "receipt_date": "2026-09-30"})
    with pytest.raises(KontorError, match=f"Beleg {rid} \\(Hosting, 2026-09-30\\) unbestätigt"):
        va.werte(september, "2026-09")
    assert va.werte(september, "2026-08")["kz83"] == 0                     # anderer Monat: nicht betroffen
    md.set_profile(september, {"ustva_eric": "nein"})
    with pytest.raises(KontorError, match="Profilschalter"):
        va.werte(september, "2026-08")
    md.set_profile(september, {"ustva_eric": "ja"})
    md.set_settings(september, {"ustva_period": "jahr"})
    with pytest.raises(KontorError, match="Voranmeldezeitraum: Jahr"):
        va.werte(september, "2026-08")


def test_auftrag_erklaert_und_nur_der_mensch_sendet(september, ui, eric_antwortet, tmp_path):
    md.set_settings(september, {"elster_hersteller_id": "00000", "elster_zertifikat": str(tmp_path / "z.pfx")})
    t = auftraege.anlegen(september, "ustva", {"zeitraum": "2026-09"}, test=True)
    a = auftraege.anlegen(september, "ustva", {"zeitraum": "2026-09"})
    assert a["titel"] == "Umsatzsteuer-Voranmeldung September 2026"
    assert a["erklaerung"].startswith("Meldet dem Finanzamt die Umsatzsteuer für September 2026:")
    assert "Umsätze zu 19 % 100 € (Steuer 19,00 €)" in a["erklaerung"]
    assert "Reverse Charge) 9 €, Steuer 1,88 €" in a["erklaerung"]
    assert "Zu zahlen: 19,00 € bis 12.10.2026 (schon fällig)." in a["erklaerung"]
    with pytest.raises(HumanOnly):
        auftraege.freigeben(september, [a["id"]], "123456")
    out = auftraege.freigeben(ui, [t["id"], a["id"]], "123456")
    assert [o["ok"] for o in out] == [True, True]
    (art, pin, test_xml), (_, _, echt_xml) = eric_antwortet
    assert art == "UStVA_2026" and pin == "123456"
    assert "<Testmerker>" in test_xml and "<Testmerker>" not in echt_xml and "<Kz83>19.00</Kz83>" in echt_xml
    gesendet = va.uebermittlungen(september, "2026-09")
    assert [(g["test"], g["kz83"]) for g in gesendet] == [(False, 1900), (True, 1900)]
    assert all((september.data / "archiv" / "elster" / g["datensatz"]).is_file() for g in gesendet)
    # nach der echten Übermittlung ist jede weitere eine Berichtigung
    assert va.werte(september, "2026-09")["berichtigt"]
    b = auftraege.anlegen(september, "ustva", {"zeitraum": "2026-09"})
    assert "berichtigte Anmeldung" in b["erklaerung"]


def test_geaenderte_werte_gehen_nicht_raus(september, ui, eric_antwortet, tmp_path):
    md.set_settings(september, {"elster_hersteller_id": "00000", "elster_zertifikat": str(tmp_path / "z.pfx")})
    a = auftraege.anlegen(september, "ustva", {"zeitraum": "2026-09"})
    receipt(september, ui, "Hosting", receipt_date="2026-09-30", gross=11900, tax_kind="inland_19", paid_on="2026-09-30")
    out = auftraege.freigeben(ui, [a["id"]], "123456")
    assert not out[0]["ok"] and auftraege.get(september, a["id"])["status"] == "veraltet" and eric_antwortet == []


def test_cli_legt_den_auftrag_an(september, eric_antwortet, capsys):
    import json

    from kontor.cli.main import main
    assert main(["auftrag", "ustva", "--zeitraum", "2026-09", "--test"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["art"] == "ustva" and out["titel"] == "Testversand: Umsatzsteuer-Voranmeldung September 2026"


def test_monatsabschluss_zeigt_schritte_und_herleitung(september, ui, eric_antwortet, tmp_path):
    """Der Monatsabschluss in fünf Schritten und in Klartext, woraus sich die Zahllast ergibt."""
    from kontor.reports.monatsabschluss import monatsabschluss
    from kontor.services import periods as per
    md.set_settings(september, {"elster_hersteller_id": "00000", "elster_zertifikat": str(tmp_path / "z.pfx")})
    m = monatsabschluss(september, "2026-09")
    assert [s["schluessel"] for s in m["schritte"]] == ["belege", "einnahmen", "voranmeldung", "zahlung", "gesperrt"]
    assert [s["stand"] for s in m["schritte"]] == ["ok", "ok", "offen", "offen", "offen"] and m["offen"] == 3
    b = m["herleitung"]
    assert b["umsatzsteuer"]["summe"] == 1900 and b["umsatzsteuer"]["posten"][0]["steuer"] == 1900
    assert b["reverse_charge"]["summe"] == 188 and b["vorsteuer"]["summe"] == 0
    assert b["zahllast"] == 1900 and b["rundung"] == 0

    a = auftraege.anlegen(september, "ustva", {"zeitraum": "2026-09"})
    auftraege.freigeben(ui, [a["id"]], "123456")
    pay.record_tax_payment(september, "2026-11-03", "-19,00", "USt 2026-09")
    per.lock_month(ui, "2026-09", "abgegeben")
    m = monatsabschluss(september, "2026-09")
    assert [s["stand"] for s in m["schritte"]] == ["ok"] * 5 and m["offen"] == 0
    v = next(s for s in m["schritte"] if s["schluessel"] == "voranmeldung")
    assert v["transferticket"] == "TT1" and v["auftrag"] == a["id"] and "unverändert" in v["text"]


def test_monatsabschluss_warnt_bei_aenderung_nach_dem_versand(september, ui, eric_antwortet, tmp_path):
    from kontor.reports.monatsabschluss import monatsabschluss
    md.set_settings(september, {"elster_hersteller_id": "00000", "elster_zertifikat": str(tmp_path / "z.pfx")})
    auftraege.freigeben(ui, [auftraege.anlegen(september, "ustva", {"zeitraum": "2026-09"})["id"]], "123456")
    receipt(september, ui, "Hosting", receipt_date="2026-09-30", gross=11900, tax_kind="inland_19", paid_on="2026-09-30")
    v = next(s for s in monatsabschluss(september, "2026-09")["schritte"] if s["schluessel"] == "voranmeldung")
    assert v["stand"] == "warn" and "Berichtigung" in v["text"]
    pay.record_tax_payment(september, "2026-11-03", "-5,00", "USt 2026-09")
    z = next(s for s in monatsabschluss(september, "2026-09")["schritte"] if s["schluessel"] == "zahlung")
    assert z["stand"] == "warn" and "5,00 €" in z["text"]


def test_monatsabschluss_seite(september, ui):
    from fastapi.testclient import TestClient

    from kontor.api.app import create_app
    with TestClient(create_app(september.data), base_url="http://127.0.0.1/m/lokal") as t:
        page = t.get("/abschluss?zeitraum=2026-09").text
        assert "Monatsabschluss September 2026" in page and "Noch 3 Schritte" in page
        assert "Umsatzsteuer auf deine Einnahmen" in page and "Community" in page
        assert "/abschluss?zeitraum=2026-09" in t.get("/umsatzsteuer?jahr=2026").text


def test_monatsabschluss_sperren_auf_der_seite(september, ui, eric_antwortet, tmp_path):
    from fastapi.testclient import TestClient

    from kontor.api.app import create_app
    from kontor.services import periods as per
    md.set_settings(september, {"elster_hersteller_id": "00000", "elster_zertifikat": str(tmp_path / "z.pfx")})
    auftraege.freigeben(ui, [auftraege.anlegen(september, "ustva", {"zeitraum": "2026-09"})["id"]], "123456")
    app = create_app(september.data)
    with TestClient(app, base_url="http://127.0.0.1/m/lokal") as t:
        page = t.get("/abschluss?zeitraum=2026-09").text
        assert "überfällig" in page and 'value="-19,00"' in page and "September 2026 sperren" not in page
        t.post("/auswertungen/ust-zahlung", data={"datum": "2026-11-03", "betrag": "-19,00", "notiz": "USt 2026-09",
                                                  "zurueck": "/abschluss?zeitraum=2026-09"})
        page = t.get("/abschluss?zeitraum=2026-09").text
        assert "September 2026 sperren" in page and "überfällig" not in page
        assert t.post("/abschluss/sperren", data={"zeitraum": "2026-09"}).status_code == 403
        t.post("/abschluss/sperren", data={"zeitraum": "2026-09", "confirm": app.state.confirm["lokal"]})
        assert "Abgeschlossen" in t.get("/abschluss?zeitraum=2026-09").text
    assert "2026-09" in {l["month"] for l in per.locked_months(september)}
