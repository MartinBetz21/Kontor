"""E-Bilanz über ERiC (Spec 18.9). Die Logik testen erfundene Salden; ERiC selbst wird hier nicht aufgerufen."""
import re
from datetime import date

import pytest

from kontor.core.errors import HumanOnly, KontorError
from kontor.elster import ebilanz as eb
from kontor.importers.lexware import parse_saldenliste
from kontor.services import elster
from kontor.services import masterdata as md

SALDEN = """﻿Konto;Bezeichnung;Saldo;S/H
1200;Forderungen;1190,00;S
1406;Vorsteuer 19 %;95,00;S
1440;Bitcoin;3000,00;S
1807;Bank;11250,00;S
2900;Gezeichnetes Kapital;12500,00;H
2978;Verlustvortrag;1000,00;S
3095;Rückstellungen Abschluss;800,00;H
3300;Verbindlichkeiten LuL;910,00;H
3730;Lohnsteuer;100,00;H
3806;Umsatzsteuer 19 %;380,00;H
4338;Erlöse Drittland;3000,00;H
4400;Erlöse 19 %;2000,00;H
4840;Kursgewinne;10,00;H
6024;Geschäftsführergehalt;1000,00;S
6670;Reisekosten Unternehmer;200,00;S
6644;Bewirtung nicht abzugsfähig;30,00;S
6837;Lizenzen;500,00;S
6960;Abschreibung Bitcoin;400,00;S
6969;Sonstige Aufwendungen;1035,00;S
"""
STAMM = eb.Stammdaten("Muster GmbH", "Musterweg", "1", "50667", "Köln", "5214012345670", 2500000, 1250000)


def bericht(year=2025):
    bal, names = parse_saldenliste(SALDEN)
    return eb.Bericht(year, bal, names, date(2026, 10, 2))


def fact(xml: str, concept: str) -> str | None:
    m = re.search(rf"<de-gaap-ci:{re.escape(concept)} [^>]*>([^<]*)</de-gaap-ci:{re.escape(concept)}>", xml)
    return m.group(1) if m else None


def test_values_roll_up_and_match_the_balance_sheet():
    total, nachweis = eb.werte(bericht(), STAMM)
    # Aktiva: 1190 + 3000 (Bitcoin) + 11250; USt netto 380 − 95 = 285 Verbindlichkeit
    assert total["bs.ass"] == 1544000 == total["bs.eqLiab"]
    assert total["bs.ass.currAss.receiv.trade"] == 119000
    assert total["bs.ass.currAss.cashEquiv"] == 1125000
    assert total["bs.eqLiab.liab.other.theroffTax"] == 10000 + 28500
    # Ergebnis: 5010 Erträge − 1000 − 200 − 30 − 500 − 400 − 1035 = 1845
    assert total["ismi.netIncome"] == 184500 == total["bs.eqLiab.equity.netIncome"] == total["BVV.profitLoss"]
    assert total["ismi.netIncome.netSales"] == 500000 and total["ismi.netIncome.deprAmort"] == 40000
    assert total["bs.eqLiab.equity"] == 2500000 - 1250000 - 100000 + 184500
    assert total["BVV.profitLoss.assetsPreviousYear.assets"] == 2500000 - 1250000 - 100000
    assert ("is.netIncome.regular.operatingTC.otherCost.employee", "6670", 20000) in nachweis   # GF-Reisen
    assert ("bs.eqLiab.liab.other.theroffTax", "1406", -9500) in nachweis                      # USt netto
    assert not any(k in ("2900", "2978") for _, k, _ in nachweis)


def test_dataset_carries_required_fields_test_flag_and_account_list():
    xml = eb.datensatz(bericht(2025), STAMM, "12345", True, "Erika Mustermann")
    assert "<Testmerker>700000004</Testmerker>" in xml and "<HerstellerID>12345</HerstellerID>" in xml
    assert '<Empfaenger id="F">5214</Empfaenger>' in xml and "<ebilanz:stichtag>20251231</ebilanz:stichtag>" in xml
    assert fact(xml, "bs.ass") == "15440.00" and fact(xml, "ismi.netIncome") == "1845.00"
    assert '<de-gaap-ci:bs.ass.fixAss.intan.goodwill xsi:nil="true" contextRef="I-AKTJAHR" unitRef="EUR"/>' in xml
    assert '<de-gaap-ci:kke xsi:nil="true" contextRef="I-AKTJAHR"/>' in xml                       # kein Geldbetrag
    assert "reversal4PrevYear" not in xml                       # gilt nur bis 2023
    assert xml.count("<de-gaap-ci:detailedInformation.accountBalances>") == 17
    assert "reportElements.KS " in xml


def test_no_test_flag_and_no_account_list_before_2025():
    xml = eb.datensatz(bericht(2024), STAMM, "12345", False, "Erika Mustermann")
    assert "Testmerker" not in xml and "accountBalances" not in xml and "reportElements.KS " not in xml


def test_positions_foreign_to_a_corporation_are_refused():
    with pytest.raises(KontorError, match="Kapitalgesellschaft"):
        eb.hochrechnen({"is.netIncome.regular.operatingTC.otherCost.businessTravelOwner": 100})


def test_unmapped_account_and_unbalanced_list_are_refused():
    bal, _ = parse_saldenliste(SALDEN)
    with pytest.raises(KontorError, match="geht nicht auf"):
        eb.werte(eb.Bericht(2025, {**bal, "1807": bal["1807"] + 1}), STAMM)


@pytest.fixture
def gmbh(ctx):
    md.set_profile(ctx, {"gewinnermittlung": "bilanz", "kontenrahmen": "skr04", "e_bilanz": "ja"})
    md.set_settings(ctx, {"name": "Muster GmbH", "street": "Musterweg 1a", "postcode": "50667", "city": "Köln",
                          "tax_number": "214/1234/5670", "share_capital": "25000,00",
                          "share_capital_outstanding": "12500,00"})
    return ctx


def test_saldenliste_is_stored_and_figures_need_no_eric(gmbh):
    elster.saldenliste_ablegen(gmbh, 2025, SALDEN)
    assert elster.jahre(gmbh) == [2025]
    k = elster.kennzahlen(gmbh, 2025)
    assert (k["bilanzsumme"], k["jahresergebnis"], k["kontennachweis"]) == (1544000, 184500, 17)
    with pytest.raises(KontorError, match="Finanzamtsnummer"):
        elster.pruefen(gmbh, 2025)


def test_sending_is_for_humans_only(gmbh):
    elster.saldenliste_ablegen(gmbh, 2025, SALDEN)
    with pytest.raises(HumanOnly):
        elster.senden(gmbh, 2025, "1234", test=True)


def test_sending_needs_manufacturer_id_and_a_closed_year(gmbh, ui):
    elster.saldenliste_ablegen(gmbh, 2025, SALDEN)
    with pytest.raises(KontorError, match="Hersteller-ID"):
        elster.senden(ui, 2025, "1234", test=True)
    with pytest.raises(KontorError, match="läuft noch"):
        elster.senden(ui, 2026, "1234", test=True)


def test_abgaben_page(tmp_path):
    from fastapi.testclient import TestClient

    from kontor.api.app import create_app
    from kontor.context import open_ctx
    ctx = open_ctx("cli", tmp_path / "d")
    md.set_profile(ctx, {"gewinnermittlung": "bilanz", "e_bilanz": "ja"})
    md.set_settings(ctx, {"name": "Muster GmbH", "street": "Musterweg 1", "postcode": "50667", "city": "Köln",
                          "share_capital": "25000,00", "share_capital_outstanding": "12500,00"})
    elster.saldenliste_ablegen(ctx, 2025, SALDEN)
    ctx.conn.close()
    with TestClient(create_app(tmp_path / "d"), base_url="http://127.0.0.1/m/lokal") as c:
        assert ">Abgaben</a>" in c.get("/").text
        page = c.get("/abgaben").text
        assert "15.440,00" in page and "Senden braucht die eigene Hersteller-ID" in page
        assert "Finanzamtsnummer" in c.post("/abgaben/2025/pruefen").text
