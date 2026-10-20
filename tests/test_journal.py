"""Journal, Eröffnung, Lexware-Kontenimport, Bilanz/GuV, Auffälligkeiten (Spec 18.8, 18.8a).
Alle Zahlen und Namen sind erfunden."""
import pytest

from kontor.core.bilanz import positions
from kontor.core.errors import HumanOnly, KontorError
from kontor.importers.lexware import parse_kontenuebersicht, parse_saldenliste
from kontor.reports.abschluss import auffaelligkeiten, bilanz_guv, saldenliste_csv
from kontor.services import journal as jr
from kontor.services import masterdata as md

SALDEN = """﻿Konto;Bezeichnung;Saldo;S/H
1200;Forderungen;1190,00;S
1406;Vorsteuer 19 %;95,00;S
1807;Bank;11250,00;S
2900;Gezeichnetes Kapital;12500,00;H
2978;Verlustvortrag;1000,00;S
3806;Umsatzsteuer 19 %;380,00;H
3816;Umsatzsteuer nicht fällig;190,00;H
4400;Erlöse 19 %;2000,00;H
6837;Lizenzen;500,00;S
6969;Sonstige Aufwendungen;1035,00;S
"""

KONTEN = '﻿"Konto";"Kontobezeichnung";"Datum";"Belegnummer";"Text";"Gegenkonto";"Soll";"Haben";"Steuer";"Steuerkonto"\n' + "\n".join([
    '"1807";"Hauptkonto";"05.02.2026";"";"DESCRIPT\nINC";"6837";"0,00";"119,00";"";""',
    '"6837";"Lizenzen";"05.02.2026";"";"DESCRIPT\nINC";"1807";"100,00";"0,00";"19,00";"1406"',
    '"1406";"Vorsteuer 19 %";"05.02.2026";"";"DESCRIPT\nINC";"1807";"19,00";"0,00";"";""',
    '"1807";"Hauptkonto";"10.03.2026";"";"Kunde zahlt RE-2026-0001";"4400";"1190,00";"0,00";"";""',
    '"4400";"Erlöse 19 %";"10.03.2026";"";"Kunde zahlt RE-2026-0001";"1807";"0,00";"1000,00";"19,00";"3806"',
    '"3806";"Umsatzsteuer 19 %";"10.03.2026";"";"Kunde zahlt RE-2026-0001";"1807";"0,00";"190,00";"";""',
    '"1200";"Forderungen";"11.03.2026";"RE-2026-0001";"RE-2026-0001";"1370";"0,00";"1190,00";"";""',
    '"1370";"Durchlaufende Posten";"11.03.2026";"RE-2026-0001";"RE-2026-0001";"1200";"1190,00";"0,00";"";""',
    '"10002";"Kunde";"11.03.2026";"RE-2026-0001";"RE-2026-0001";"1370";"0,00";"1190,00";"";""',
    '"1807";"Hauptkonto";"12.03.2026";"";"Erika Mustermann";"6969";"0,00";"200,00";"";""',
    '"6969";"Sonstige Aufwendungen";"12.03.2026";"";"Erika Mustermann";"1807";"200,00";"0,00";"";""',
    '"1809";"Steuerkonto";"13.03.2026";"";"DE94123456781111111111";"1460";"200,00";"0,00";"";""',
    '"1460";"Geldtransit";"13.03.2026";"";"DE94123456781111111111";"1809";"0,00";"200,00";"";""',
]) + "\n"


@pytest.fixture
def gmbh(ctx):
    md.set_profile(ctx, {"gewinnermittlung": "bilanz", "kontenrahmen": "skr04"})
    md.set_settings(ctx, {"share_capital": "25000,00", "share_capital_outstanding": "12500,00",
                          "lexware_journal_years": "2026",                  # Journal 2026 aus Lexware (18.8a)
                          "owner_names": "Erika Mustermann", "foreign_providers": "DESCRIPT"})
    return ctx


# -- Parser -------------------------------------------------------------------
def test_saldenliste_is_parsed_signed_in_cents():
    bal, names = parse_saldenliste(SALDEN)
    assert bal["1200"] == 119000 and bal["2900"] == -1250000 and names["1807"] == "Bank"


def test_kontenuebersicht_survives_line_breaks_and_skips_personal_accounts():
    rows = parse_kontenuebersicht(KONTEN)
    assert len(rows) == 12                                    # 13 Zeilen, Debitor 10002 fällt weg
    assert rows[0]["text"] == "DESCRIPT INC" and rows[0]["amount"] == -11900 and rows[0]["date"] == "2026-02-05"
    assert rows[1]["tax_rate"] == "19" and rows[1]["tax_account"] == "1406"


# -- Bilanzposten ---------------------------------------------------------------
def test_positions_balance_and_net_the_vat():
    bal, _ = parse_saldenliste(SALDEN)
    p = positions(bal, 2500000, 1250000)
    assert p["erg"] == 46500                                  # 2.000 − 500 − 1.035
    assert p["aktiva"] == p["passiva"] == 1244000              # Forderung + Bank; USt netto als Schuld
    assert p["ek"] == 2500000 - 1250000 - 100000 + 46500


def test_positions_refuse_an_unbalanced_list():
    bal, _ = parse_saldenliste(SALDEN)
    bal["1807"] += 1
    with pytest.raises(KontorError, match="geht nicht auf"):
        positions(bal, 2500000, 1250000)


# -- Eröffnung und Import ---------------------------------------------------------
def test_opening_carries_balances_and_moves_result_and_vat(gmbh):
    jr.opening_from_saldenliste(gmbh, 2026, SALDEN)
    bal = jr.balances(gmbh, 2026)
    assert bal["1807"] == 1125000 and bal["3816"] == -19000
    assert "4400" not in bal and bal["2978"] == 100000 - 46500        # Ergebnis 465,00 in den Vortrag
    assert "3806" not in bal and "1406" not in bal and bal["3841"] == -38000 + 9500
    r = bilanz_guv(gmbh, 2026, "2026-01-01")
    assert r["positionen"]["erg"] == 0 and r["positionen"]["aktiva"] == 1244000


def test_opening_only_once(gmbh):
    jr.opening_from_saldenliste(gmbh, 2026, SALDEN)
    with pytest.raises(KontorError, match="Eröffnung"):
        jr.opening_from_saldenliste(gmbh, 2026, SALDEN)


def test_lexware_import_replaces_the_previous_import_of_the_year(gmbh):
    jr.opening_from_saldenliste(gmbh, 2026, SALDEN)
    assert jr.import_lexware(gmbh, 2026, KONTEN) == 12
    assert jr.import_lexware(gmbh, 2026, KONTEN) == 12
    bal = jr.balances(gmbh, 2026)
    assert bal["1807"] == 1125000 - 11900 + 119000 - 20000
    assert bal["4400"] == -100000


def test_import_refuses_rows_from_another_year(gmbh):
    with pytest.raises(KontorError, match="2025"):
        jr.import_lexware(gmbh, 2025, KONTEN)


# -- Buchungen von Hand -----------------------------------------------------------
def test_manual_entry_needs_text_and_must_balance(gmbh):
    with pytest.raises(KontorError, match="Begründung"):
        jr.book(gmbh, {"ref": "K1", "date": "2026-12-31", "text": "", "lines": {"6969": 100, "1370": -100}})
    with pytest.raises(KontorError, match="null"):
        jr.book(gmbh, {"ref": "K1", "date": "2026-12-31", "text": "Test", "lines": {"6969": 100, "1370": -99}})
    jr.book(gmbh, {"ref": "K1", "date": "2026-12-31", "text": "Ausgleich", "lines": {"6969": "1,00", "1370": "-1,00"}})
    assert jr.balances(gmbh, 2026)["6969"] == 100


def test_locked_year_is_fixed(gmbh, ui):
    jr.book(gmbh, {"ref": "K1", "date": "2026-12-31", "text": "Ausgleich", "lines": {"6969": 100, "1370": -100}})
    with pytest.raises(HumanOnly):
        jr.lock_year(gmbh, 2026, "Abschluss")
    jr.lock_year(ui, 2026, "Abschluss aufgestellt")
    with pytest.raises(KontorError, match="gesperrt"):
        jr.book(gmbh, {"ref": "K2", "date": "2026-12-31", "text": "zu spät", "lines": {"6969": 100, "1370": -100}})
    with pytest.raises(KontorError, match="gesperrt"):
        jr.import_lexware(gmbh, 2026, KONTEN)


# -- Auffälligkeiten (Buchungsfallen) ----------------------------------------------------
def test_findings_name_the_known_traps(gmbh):
    jr.opening_from_saldenliste(gmbh, 2026, SALDEN)
    jr.import_lexware(gmbh, 2026, KONTEN)
    arten = {f["art"]: f for f in auffaelligkeiten(gmbh, 2026)}
    assert arten["verrechnungskonto"]["konto"] in ("1370", "1460")
    assert arten["vorsteuer_ausland"]["betrag"] == 1900                       # Nr. 13: DESCRIPT
    assert arten["erloes_doppelt"]["betrag"] == 119000                        # Nr. 12
    assert arten["umbuchung_als_aufwand"]["betrag"] == 20000                  # Nr. 11


def test_export_duplicates_are_found(gmbh):
    dup = KONTEN + '"1807";"Hauptkonto";"05.02.2026";"";"DESCRIPT\nINC";"6837";"0,00";"119,00";"";""\n'
    jr.import_lexware(gmbh, 2026, dup)
    assert any(f["art"] == "exportdublette" for f in auffaelligkeiten(gmbh, 2026))


def test_saldenliste_csv_round_trips(gmbh):
    jr.opening_from_saldenliste(gmbh, 2026, SALDEN)
    bal, _ = parse_saldenliste(saldenliste_csv(gmbh, 2026))
    assert bal == jr.balances(gmbh, 2026)


# -- Oberfläche und Kommandozeile --------------------------------------------------
def test_journal_page_and_account_sheet(tmp_path):
    from fastapi.testclient import TestClient

    from kontor.api.app import create_app
    from kontor.context import open_ctx
    ctx = open_ctx("cli", tmp_path / "d")
    md.set_profile(ctx, {"gewinnermittlung": "bilanz"})
    md.set_settings(ctx, {"share_capital": "25000,00", "share_capital_outstanding": "12500,00",
                          "lexware_journal_years": "2026", "foreign_providers": "DESCRIPT"})
    jr.opening_from_saldenliste(ctx, 2026, SALDEN)
    jr.import_lexware(ctx, 2026, KONTEN)
    ctx.conn.close()
    app = create_app(tmp_path / "d")
    with TestClient(app, base_url="http://127.0.0.1/m/lokal") as c:
        assert "Journal &amp; Bilanz" in c.get("/").text
        page = c.get("/journal?jahr=2026").text
        assert "Summe Aktiva" in page and "§ 13b, die Vorsteuer ist Kosten" in page
        assert "DESCRIPT INC" in c.get("/journal/konto/1807?jahr=2026").text
        r = c.post("/journal/sperren", data={"jahr": "2026", "vermerk": "Test", "confirm": app.state.confirm["lokal"]},
                   follow_redirects=False)
        assert r.status_code == 303


def test_euer_mandant_has_no_journal(ui):
    with pytest.raises(KontorError, match="Bilanzierung"):
        jr.opening_from_saldenliste(ui, 2026, SALDEN)


def test_cli_journal_round_trip(tmp_path, monkeypatch, capsys):
    import json

    from kontor.cli.main import main
    monkeypatch.setenv("KONTOR_DATA", str(tmp_path / "d"))
    (tmp_path / "s.csv").write_text(SALDEN, encoding="utf-8")
    (tmp_path / "k.csv").write_text(KONTEN, encoding="utf-8")
    (tmp_path / "p.json").write_text(json.dumps({"gewinnermittlung": "bilanz"}))
    (tmp_path / "e.json").write_text(json.dumps({"share_capital": "25000,00", "share_capital_outstanding": "12500,00",
                                                     "lexware_journal_years": "2026"}))
    for argv in (["profil", "setzen", "--json", str(tmp_path / "p.json")],
                 ["einstellungen", "setzen", "--json", str(tmp_path / "e.json")],
                 ["journal", "eroeffnung", "--jahr", "2026", "--saldenliste", str(tmp_path / "s.csv")],
                 ["journal", "lexware", "--jahr", "2026", "--datei", str(tmp_path / "k.csv")]):
        assert main(argv) == 0, capsys.readouterr()
    capsys.readouterr()
    assert main(["auswertung", "bilanz", "2026"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["positionen"]["aktiva"] == out["positionen"]["passiva"]
