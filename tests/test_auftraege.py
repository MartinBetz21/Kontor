"""Aufträge zur Freigabe: anlegen (auch der Agent), prüfen, freigeben nur durch den Menschen, veraltete Werte gehen
nicht raus. Die Übermittlung selbst braucht ELSTER; hier sendet eine Probe-Art, die nur antwortet."""
import os
from datetime import date

import pytest

from kontor.core.errors import HumanOnly, KontorError
from kontor.services import auftraege
from kontor.services import lohn
from kontor.services import masterdata as md

ERIC = os.environ.get("KONTOR_ERIC", "")


@pytest.fixture
def probe(monkeypatch):
    """Eine Auftragsart ohne Behörde: Werte aus einem Dict, Senden merkt sich nur den Aufruf."""
    zustand = {"werte": {"betrag": 100}, "gesendet": []}

    def senden(ctx, p, pin, test):
        zustand["gesendet"].append((p["nr"], pin, test))
        return {"ok": True, "code": 0, "text": "Verarbeitung fehlerfrei.", "meldungen": [], "transferticket": f"T{p['nr']}"}

    monkeypatch.setitem(auftraege.ARTEN, "probe", auftraege.Art(
        "Probe", lambda ctx, p: dict(zustand["werte"]), lambda ctx, p, w: f"Probe {p['nr']}",
        lambda ctx, p, w: f"Sendet {w['betrag']} Cent.", lambda ctx, p: {"ok": True, "text": "", "meldungen": []}, senden))
    return zustand


def test_agent_prepares_human_approves_in_one_go(ctx, ui, probe):
    a = auftraege.anlegen(ctx, "probe", {"nr": 1}, notiz="Oktober")
    b = auftraege.anlegen(ctx, "probe", {"nr": 2}, test=True)
    assert a["titel"] == "Probe 1" and a["erklaerung"] == "Sendet 100 Cent." and a["erstellt_von"] == "cli"
    assert b["titel"].startswith("Testversand: ") and "zählt nicht" in b["erklaerung"]
    with pytest.raises(KontorError, match="schon offen"):
        auftraege.anlegen(ctx, "probe", {"nr": 1})
    with pytest.raises(HumanOnly):
        auftraege.freigeben(ctx, [a["id"]], "1234")
    with pytest.raises(KontorError, match="PIN"):
        auftraege.freigeben(ui, [a["id"]], "")
    out = auftraege.freigeben(ui, [b["id"], a["id"]], "1234")
    assert [o["transferticket"] for o in out] == ["T1", "T2"] and probe["gesendet"] == [(1, "1234", False), (2, "1234", True)]
    assert auftraege.offen_anzahl(ctx) == 0 and auftraege.get(ctx, a["id"])["status"] == "gesendet"
    assert auftraege.freigeben(ui, [a["id"]], "1234")[0]["text"] == "nicht offen (gesendet)"


def test_changed_values_are_not_sent(ctx, ui, probe):
    a = auftraege.anlegen(ctx, "probe", {"nr": 1})
    probe["werte"]["betrag"] = 200                                          # z. B. Abrechnung storniert und neu
    out = auftraege.freigeben(ui, [a["id"]], "1234")
    assert not out[0]["ok"] and "geändert" in out[0]["text"] and probe["gesendet"] == []
    assert auftraege.get(ctx, a["id"])["status"] == "veraltet"


def test_discard_needs_a_reason_and_only_open_jobs(ctx, probe):
    a = auftraege.anlegen(ctx, "probe", {"nr": 1})
    with pytest.raises(KontorError, match="Grund"):
        auftraege.verwerfen(ctx, a["id"], "")
    auftraege.verwerfen(ctx, a["id"], "doppelt")
    assert auftraege.get(ctx, a["id"])["ergebnis"] == {"grund": "doppelt"}
    with pytest.raises(KontorError, match="offene"):
        auftraege.verwerfen(ctx, a["id"], "nochmal")
    with pytest.raises(KontorError, match="Auftragsart"):
        auftraege.anlegen(ctx, "umsatzsteuer-raten", {})


def test_page_lists_jobs_and_sends_only_with_confirm(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from kontor.api.app import create_app
    from kontor.context import open_ctx
    c = open_ctx("cli", tmp_path / "d")
    md.set_settings(c, {"elster_hersteller_id": "00000", "elster_zertifikat": str(tmp_path / "x.pfx")})
    gesendet = []
    monkeypatch.setitem(auftraege.ARTEN, "probe", auftraege.Art(
        "Probe", lambda ctx, p: {}, lambda ctx, p, w: "Probe-Auftrag", lambda ctx, p, w: "Erklärung in einfacher Sprache.",
        None, lambda ctx, p, pin, test: gesendet.append(pin) or {"ok": True, "code": 0, "text": "fehlerfrei",
                                                                "meldungen": [], "transferticket": "TT"}))
    a = auftraege.anlegen(c, "probe", {"nr": 1})
    c.conn.close()
    app = create_app(tmp_path / "d")
    with TestClient(app, base_url="http://127.0.0.1/m/lokal") as t:
        home = t.get("/").text
        assert "Freigaben" in home and '<span class="badge">1</span>' in home
        page = t.get("/freigaben").text
        assert "Probe-Auftrag" in page and "Erklärung in einfacher Sprache." in page and "Claude" in page
        assert t.post("/freigaben", data={"auftrag": a["id"], "pin": "1", "datenschutz": "1"}).status_code == 403
        r = t.post("/freigaben", data={"auftrag": a["id"], "pin": "1", "confirm": app.state.confirm["lokal"]})
        assert "Datenschutzhinweise" in r.text and gesendet == []
        r = t.post("/freigaben", data={"auftrag": a["id"], "pin": "1", "datenschutz": "1",
                                       "confirm": app.state.confirm["lokal"]})
        assert gesendet == ["1"] and "TT" in r.text and "Nichts zur Freigabe offen" in r.text


@pytest.mark.skipif(not ERIC, reason="KONTOR_ERIC nicht gesetzt")
def test_real_job_for_the_wage_tax_return(ctx, ui):
    md.set_profile(ctx, {"lohn": "ja"})
    md.set_settings(ctx, {"name": "Muster GmbH", "street": "Musterweg 1", "postcode": "80333", "city": "München",
                          "tax_number": "198/113/10134", "finanzamt_nr": "9198", "eric_pfad": ERIC,
                          "lohn_finanzamt_name": "Testfinanzamt"})
    ctx.today = ui.today = date(2026, 11, 2)
    mid = lohn.mitarbeiter_speichern(ctx, {"personalnr": "1", "vorname": "Erika", "nachname": "Muster",
                                           "geburtsdatum": "1980-05-10", "eintritt": "2026-10-01",
                                           "steuer_id": "01127645382"})
    lohn.merkmale_setzen(ctx, mid, "2026-10", {"steuerklasse": 5, "personengruppe": "", "beitragsgruppe": "0000"})
    lohn.bezug(ctx, mid, {"lohnart": "gehalt", "betrag": "1,00", "ab": "2026-10"})
    with pytest.raises(KontorError, match="nicht alles festgeschrieben"):
        auftraege.anlegen(ctx, "lsta", {"jahr": 2026, "zeitraum": "10"})
    lohn.festschreiben(ctx, mid, "2026-10")
    a = auftraege.anlegen(ctx, "lsta", {"jahr": 2026, "zeitraum": "10"})
    assert a["titel"] == "Lohnsteuer-Anmeldung Oktober 2026"
    assert a["erklaerung"].startswith("Meldet Testfinanzamt die Lohnsteuer für Oktober 2026: 1 Arbeitnehmer")
    assert "Zu zahlen: 0,00 € bis 10.11.2026." in a["erklaerung"]
    w = auftraege.anlegen(ctx, "elstam", {"vorgang": "wechsel", "mitarbeiter": mid, "referenzdatum": "2026-10-01"},
                          test=True)
    assert "ab 01.10.2026 Kontor statt des bisherigen Lohnprogramms" in w["erklaerung"]
    assert auftraege.anlegen(ctx, "elstam_abholen", {})["titel"] == "ELStAM aus dem ELSTER-Postfach abholen"


def test_wrong_pin_keeps_jobs_open_and_stops(ctx, ui, probe, monkeypatch):
    """Eine falsche PIN scheitert vor dem Versand: der Auftrag bleibt offen, die übrigen werden nicht mit derselben
    PIN versucht (jede Fehleingabe zählt am Zertifikat)."""
    a = auftraege.anlegen(ctx, "probe", {"nr": 1})
    b = auftraege.anlegen(ctx, "probe", {"nr": 2})
    versuche = []

    def falsch(ctx, p, pin, test):
        versuche.append(p["nr"])
        return {"ok": False, "code": 610201106, "text": "eSigner: Es wurde ein falsches Passwort bzw. eine falsche PIN "
                "angegeben.", "meldungen": [], "transferticket": ""}
    monkeypatch.setattr(auftraege.ARTEN["probe"], "senden", falsch)
    out = auftraege.freigeben(ui, [a["id"], b["id"]], "0000")
    assert versuche == [1] and not out[0]["ok"] and "PIN" in out[0]["text"] and len(out) == 1
    assert auftraege.get(ctx, a["id"])["status"] == "offen" and auftraege.get(ctx, b["id"])["status"] == "offen"


def test_detail_page_checks_before_sending(ctx, ui, probe, monkeypatch, tmp_path):
    """Detailansicht je Auftrag: Werte mit Beschriftung, „Jetzt prüfen“ rechnet neu und lässt ERiC prüfen, ohne zu
    senden; nach dem Versand Ergebnis und ELSTER-Protokoll."""
    from fastapi.testclient import TestClient

    from kontor.api.app import create_app
    pruefungen = []
    monkeypatch.setattr(auftraege.ARTEN["probe"], "pruefen", lambda c, p: pruefungen.append(p["nr"]) or {
        "ok": True, "code": 610001003, "text": "Plausibel, mit Hinweisen.", "meldungen": ["Kz09 · Hinweis zur Probe"]})
    a = auftraege.anlegen(ctx, "probe", {"nr": 7})
    pruefungen.clear()
    app = create_app(ctx.data)
    with TestClient(app, base_url="http://127.0.0.1/m/lokal") as t:
        assert f'/freigaben/{a["id"]}"' in t.get("/freigaben").text
        page = t.get(f"/freigaben/{a['id']}").text
        assert "Probe 7" in page and "Sendet 100 Cent." in page and "betrag" in page and pruefungen == []
        page = t.post(f"/freigaben/{a['id']}/pruefen").text
        assert pruefungen == [7] and "Plausibel, mit Hinweisen." in page and "Hinweis zur Probe" in page
        assert "Werte unverändert seit dem Anlegen" in page
        probe["werte"]["betrag"] = 250
        page = t.post(f"/freigaben/{a['id']}/pruefen").text
        assert "Werte haben sich seit dem Anlegen geändert" in page and probe["gesendet"] == []
        assert auftraege.get(ctx, a["id"])["status"] == "offen"
        probe["werte"]["betrag"] = 100
        pdf = ctx.data / "archiv" / "elster" / "probe.pdf"
        pdf.parent.mkdir(parents=True, exist_ok=True)
        pdf.write_bytes(b"%PDF-1.4 probe")
        monkeypatch.setattr(auftraege.ARTEN["probe"], "senden", lambda c, p, pin, test: {
            "ok": True, "code": 0, "text": "Verarbeitung fehlerfrei.", "meldungen": [], "transferticket": "TT9",
            "protokoll": str(pdf)})
        auftraege.freigeben(ui, [a["id"]], "1234")
        page = t.get(f"/freigaben/{a['id']}").text
        assert "TT9" in page and "Jetzt prüfen" not in page
        assert t.get(f"/freigaben/{a['id']}/protokoll.pdf").content == b"%PDF-1.4 probe"
