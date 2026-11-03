"""Sozialversicherung: Sätze mit Quelle, Rundungsregeln, BBG, Übergangsbereich, Minijob, PV, Umlagen.

Alle Zahlen sind von Hand gerechnet (Formel im Kommentar) oder stammen aus amtlichen Beispielen:
- „Rundschreiben Übergangsbereich" = Gemeinsames Rundschreiben der Spitzenorganisationen der Sozialversicherung vom
  20.12.2022 zu Beschäftigungen im Übergangsbereich ab 01.01.2023 (GKV-Spitzenverband, DRV Bund, DRV KBS, BA),
  kbs.de/SharedDocs/Downloads/DE/VersicherungsrechtBeitraegeMeldungen/Downloads/Uebergangsbereich_idF_20_12_2022.pdf.
  Seine Beispiele rechnen mit den Werten von 2023 (G 520 €, F 0,6922, PV 3,05 % + 0,35 %): `_w2023()` baut sie nach.
- „BVV" = Beitragsverfahrensverordnung, gesetze-im-internet.de/beitrvv.
"""
from copy import deepcopy
from decimal import Decimal as D

import pytest

from kontor.core.errors import KontorError
from kontor.core.sozialversicherung import _rechne, beitraege, werte


def _w2023() -> dict:
    """Werte des Rundschreibens (2023): sonst gleich wie 2025: KV 14,6, RV 18,6, AV 2,6."""
    w = deepcopy(werte(2025))
    w["geringfuegigkeitsgrenze"]["wert"] = 52000
    w["faktor_f"]["wert"] = "0.6922"
    w["pv"]["wert"] = "3.05"
    w["pv_kinderlosenzuschlag"]["wert"] = "0.35"
    return w


# ---------------------------------------------------------------- Werte mit Quelle

def _blaetter(block):
    for key, v in block.items():
        yield key, v


@pytest.mark.parametrize("jahr", [2025, 2026])
def test_jeder_wert_hat_eine_quelle(jahr):
    for key, v in _blaetter(werte(jahr)):
        assert set(v) == {"wert", "quelle"}, key
        assert len(v["quelle"]) > 20, key


def test_werte_unbekanntes_jahr():
    with pytest.raises(KontorError, match="2030"):
        werte(2030)


@pytest.mark.parametrize("jahr,f,zusatz", [(2025, "0.6683", "2.5"), (2026, "0.6619", "2.9")])
def test_faktor_f_folgt_aus_den_saetzen(jahr, f, zusatz):
    # F = 28 % / (RV 18,6 + AV 2,6 + PV 3,6 + KV 14,6 + durchschnittlicher Zusatzbeitrag), vier Stellen (§ 20 Abs. 2a SGB IV)
    # 2025: 28 / 41,9 = 0,66826 → 0,6683; 2026: 28 / 42,3 = 0,66194 → 0,6619
    w = werte(jahr)
    gsv = D(w["rv"]["wert"]) + D(w["av"]["wert"]) + D(w["pv"]["wert"]) + D(w["kv_allgemein"]["wert"]) + D(w["kv_zusatz_durchschnitt"]["wert"])
    assert w["kv_zusatz_durchschnitt"]["wert"] == zusatz
    assert (D(28) / gsv).quantize(D("0.0001")) == D(w["faktor_f"]["wert"]) == D(f)


def test_grenzen_2025_und_2026():
    a, b = werte(2025), werte(2026)
    assert (a["geringfuegigkeitsgrenze"]["wert"], b["geringfuegigkeitsgrenze"]["wert"]) == (55600, 60300)
    # Geringfügigkeitsgrenze = Mindestlohn x 130 / 3, aufgerundet: 12,82 → 555,53 → 556; 13,90 → 602,33 → 603 (§ 8 Abs. 1a SGB IV)
    assert (a["bbg_kv_monat"]["wert"], a["bbg_rv_monat"]["wert"]) == (551250, 805000)
    assert (b["bbg_kv_monat"]["wert"], b["bbg_rv_monat"]["wert"]) == (581250, 845000)
    for w in (a, b):
        assert w["bbg_kv_jahr"]["wert"] == 12 * w["bbg_kv_monat"]["wert"]
        assert w["bbg_rv_jahr"]["wert"] == 12 * w["bbg_rv_monat"]["wert"]
    assert (a["umlage_u1_minijob"]["wert"], b["umlage_u1_minijob"]["wert"]) == ("1.1", "0.8")


# ---------------------------------------------------------------- Regulär: Rundung, Sätze

def test_regulaer_hälftige_beitraege_2025():
    # 3.000,00 €, Zusatzbeitrag 2,5 %: KV 7,3 % = 219,00 + Zusatz 1,25 % = 37,50 → 256,50 je Seite
    # RV 9,3 % = 279,00; AV 1,3 % = 39,00; PV 1,8 % = 54,00
    r = beitraege(2025, entgelt=300000, kv_zusatz=D("2.5"))
    assert (r["kv_an"], r["kv_ag"]) == (25650, 25650)
    assert (r["rv_an"], r["rv_ag"]) == (27900, 27900)
    assert (r["av_an"], r["av_ag"]) == (3900, 3900)
    assert (r["pv_an"], r["pv_ag"]) == (5400, 5400)
    assert r["an_summe"] == 25650 + 27900 + 3900 + 5400
    assert r["insolvenz"] == 450                      # 0,15 % von 3.000,00 € = 4,50
    assert r["ag_summe"] == 25650 + 27900 + 3900 + 5400 + 450
    assert r["art"] == "regulaer"
    assert r["bemessung_kv"] == r["bemessung_rv"] == 300000


def test_rundung_halbieren_runden_verdoppeln():
    # BVV § 2 Abs. 1 Satz 1: halber Satz, runden, verdoppeln. 2.500,50 €: RV 9,3 % = 232,5465 → 232,55, je Seite.
    # Gesamt 465,10 (nicht 18,6 % = 465,093 → 465,09)
    r = beitraege(2025, entgelt=250050)
    assert r["rv_an"] == r["rv_ag"] == 23255
    assert r["rv_an"] + r["rv_ag"] == 46510


def test_zusatzbeitrag_wird_getrennt_gerundet():
    # Rundschreiben Übergangsbereich Bsp. 8: Beitrag und Zusatzbeitrag je eigens halbiert, gerundet, verdoppelt.
    # E = 836,45 €, Zusatz 1,5 %: 7,3 % = 61,05 → 61,06 (61,05585); 0,75 % = 6,27 (6,2734) → AN je 67,33
    r = beitraege(2025, entgelt=83645, kv_zusatz=D("1.5"), uebergangsbereich=False)   # regulär erzwungen
    assert r["kv_an"] == r["kv_ag"] == 6106 + 627


def test_kv_ermaessigt_und_arbeitgeberanteil_ohne_an_beitrag():
    # KV 3: 14,0 % → 7,0 % von 3.000,00 € = 210,00 je Seite. RV 3 und AV 2: nur Arbeitgeber
    # (§ 172 Abs. 1 SGB VI, § 346 Abs. 3 SGB III): RV 9,3 % = 279,00, AV 1,3 % = 39,00
    r = beitraege(2025, entgelt=300000, beitragsgruppe="3321")
    assert (r["kv_an"], r["kv_ag"]) == (21000, 21000)
    assert (r["rv_an"], r["rv_ag"]) == (0, 27900)
    assert (r["av_an"], r["av_ag"]) == (0, 3900)


def test_beitragsgruppe_null_ist_null():
    r = beitraege(2025, entgelt=300000, beitragsgruppe="0000")
    assert r["an_summe"] == 0
    assert r["ag_summe"] == 450                       # nur die Insolvenzgeldumlage: Basis „RV-Entgelt, das zu bemessen wäre"


def test_werkstudent_nur_rv():
    # 106, Beitragsgruppe 0100: 1.500,00 € → RV 9,3 % = 139,50 je Seite, sonst nichts
    r = beitraege(2025, entgelt=150000, beitragsgruppe="0100", personengruppe="106", uebergangsbereich=False)
    assert (r["rv_an"], r["rv_ag"]) == (13950, 13950)
    assert r["kv_an"] == r["kv_ag"] == r["av_an"] == r["pv_ag"] == 0


# ---------------------------------------------------------------- Pflegeversicherung

def test_pv_kinderlosenzuschlag_nur_arbeitnehmer():
    # § 55 Abs. 3 Satz 1 SGB XI: 3.000,00 €: AN 1,8 % = 54,00 + Zuschlag 0,6 % = 18,00 → 72,00; AG 54,00
    r = beitraege(2025, entgelt=300000, pv_kinderlos=True)
    assert (r["pv_an"], r["pv_ag"]) == (7200, 5400)


@pytest.mark.parametrize("kinder,abschlag_punkte", [(0, "0"), (1, "0"), (2, "0.25"), (3, "0.5"), (5, "1.0"), (7, "1.0")])
def test_pv_abschlag_ab_dem_zweiten_kind(kinder, abschlag_punkte):
    # § 55 Abs. 3 Satz 4: 0,25 Punkte je Kind vom 2. bis 5. Kind, höchstens 1,0; nur der Arbeitnehmer
    # AN = 54,00 − 3.000,00 € x Abschlag; AG bleibt 54,00
    r = beitraege(2025, entgelt=300000, pv_kinder=kinder)
    assert r["pv_ag"] == 5400
    assert r["pv_an"] == 5400 - int(D(300000) * D(abschlag_punkte) / 100)


def test_pv_sachsen():
    # § 58 Abs. 3 SGB XI: AN 2,3 % = 69,00, AG 1,3 % = 39,00 von 3.000,00 €; mit Kinderlosenzuschlag AN 87,00
    r = beitraege(2025, entgelt=300000, sachsen=True)
    assert (r["pv_an"], r["pv_ag"]) == (6900, 3900)
    r = beitraege(2025, entgelt=300000, sachsen=True, pv_kinderlos=True)
    assert (r["pv_an"], r["pv_ag"]) == (8700, 3900)


def test_pv_halber_beitragssatz_beihilfe():
    # PV-Ziffer 2: 1,8 % Gesamtsatz → 0,9 % je Seite = 27,00; Sachsen: 0,5 allein → AN 0,65 + 0,5 = 1,15 % = 34,50, AG 0,65 % = 19,50
    r = beitraege(2025, entgelt=300000, beitragsgruppe="1112")
    assert (r["pv_an"], r["pv_ag"]) == (2700, 2700)
    r = beitraege(2025, entgelt=300000, beitragsgruppe="1112", sachsen=True)
    assert (r["pv_an"], r["pv_ag"]) == (3450, 1950)


def test_pv_kinder_und_kinderlos_schliessen_sich_aus():
    with pytest.raises(KontorError, match="schließen sich aus"):
        beitraege(2025, entgelt=300000, pv_kinderlos=True, pv_kinder=2)


# ---------------------------------------------------------------- Beitragsbemessungsgrenze

def test_bbg_2025_und_2026():
    # 10.000,00 €: KV/PV auf 5.512,50 €, RV/AV auf 8.050,00 € (2025)
    # KV 7,3 % = 402,4125 → 402,41; RV 9,3 % = 748,65; AV 1,3 % = 104,65; PV 1,8 % = 99,225 → 99,23
    r = beitraege(2025, entgelt=1000000)
    assert (r["bemessung_kv"], r["bemessung_rv"]) == (551250, 805000)
    assert (r["kv_an"], r["rv_an"], r["av_an"], r["pv_an"]) == (40241, 74865, 10465, 9923)
    # 2026: 5.812,50 € und 8.450,00 €; RV 9,3 % = 785,85
    r = beitraege(2026, entgelt=1000000)
    assert (r["bemessung_kv"], r["bemessung_rv"]) == (581250, 845000)
    assert r["rv_an"] == 78585


def test_bbg_teilmonat_anteilig():
    # 15 SV-Tage: BBG x 15 / 30 → KV 2.756,25 €, RV 4.025,00 € (§ 1 Abs. 1 BVV)
    r = beitraege(2025, entgelt=1000000, sv_tage=15)
    assert (r["bemessung_kv"], r["bemessung_rv"]) == (275625, 402500)


def test_einmalzahlung_gegen_anteilige_jahres_bbg():
    # Dezember 2025: 11 Monate à 5.000,00 € bisher (330 Tage), laufend 5.000,00 €, Einmal 10.000,00 €.
    # KV: Jahres-BBG 66.150,00 − bisher 55.000,00 − laufend 5.000,00 = 6.150,00 → bemessung 11.150,00 € (§ 23a Abs. 3 SGB IV)
    # RV: Jahres-BBG 96.600,00 − 60.000,00 = 36.600,00 ≥ 10.000,00 → voll: 15.000,00 €
    r = beitraege(2025, entgelt=500000, einmal=1000000, bisher_entgelt=5500000, bisher_sv_tage=330, monat=12)
    assert r["bemessung_kv"] == 500000 + 615000
    assert r["bemessung_rv"] == 1500000


def test_maerzklausel_wird_erkannt_und_abgelehnt():
    # Einmal-Entgelt im Februar, das die anteilige BBG sprengt: gehört womöglich ins Vorjahr (§ 23a Abs. 4 SGB IV)
    args = dict(entgelt=500000, einmal=1000000, bisher_entgelt=500000, bisher_sv_tage=30)
    with pytest.raises(KontorError, match="Märzklausel"):
        beitraege(2025, monat=2, **args)
    assert beitraege(2025, monat=6, **args)["bemessung_kv"] > 0     # nach März gilt die normale Zuordnung


# ---------------------------------------------------------------- Umlagen

def test_umlagen_basis_rv_entgelt_ohne_einmal():
    # § 7 Abs. 2 AAG: U1/U2 vom laufenden Entgelt (3.000,00 €), Einmal-Entgelt (1.000,00 €) nicht.
    # § 358 Abs. 2 SGB III: Insolvenzgeldumlage vom RV-Entgelt mit Einmal (4.000,00 €).
    # U1 1,0 % = 30,00; U2 0,3 % = 9,00; IGU 0,15 % von 4.000,00 € = 6,00
    r = beitraege(2025, entgelt=300000, einmal=100000, u1=D("1.0"), u2=D("0.3"), bisher_sv_tage=30 * 5, bisher_entgelt=1500000)
    assert (r["u1"], r["u2"], r["insolvenz"]) == (3000, 900, 600)


def test_umlagen_bis_zur_rv_bbg():
    # 10.000,00 €, 2025: Basis 8.050,00 €; U1 1,1 % = 88,55; IGU 0,15 % = 12,075 → 12,08
    r = beitraege(2025, entgelt=1000000, u1=D("1.1"))
    assert (r["u1"], r["insolvenz"]) == (8855, 1208)


# ---------------------------------------------------------------- Übergangsbereich (Midijob)

def test_uebergangsbereich_bemessung_bsp5():
    # Rundschreiben Bsp. 5: 1.150,00 €: BE 1.058,08 €, BE Arbeitnehmer 851,35 €
    # BE = 0,6922·520 + (2000/1480 − 520/1480·0,6922)·630 = 359,944 + 1,108146·630 = 1.058,08
    # BE_AN = 2000/1480 · 630 = 851,35
    r = _rechne(_w2023(), entgelt=115000)
    assert (r["bemessung_kv"], r["bemessung_an_kv"]) == (105808, 85135)
    assert r["art"] == "midijob"


def test_uebergangsbereich_bsp8_kinderlos_zusatzbeitrag():
    # Rundschreiben Bsp. 8: 950,00 €, kinderlos, PV 3,05 % + 0,35 %, Zusatzbeitrag 1,5 %. BE 836,45 €, BE_AN 581,08 €
    r = _rechne(_w2023(), entgelt=95000, kv_zusatz=D("1.5"), pv_kinderlos=True)
    assert (r["bemessung_kv"], r["bemessung_an_kv"]) == (83645, 58108)
    assert r["kv_an"] == 4242 + 436 and r["kv_ag"] == 8788           # AG 87,88 € (nach Abzug von 42,42 und 4,36)
    assert r["pv_ag"] == 1666                                        # 25,52 − 8,86
    assert r["pv_an"] == 886 + 293                                   # 8,86 + Zuschlag 2,93 (836,45 · 0,35 %)
    assert (r["rv_an"], r["rv_ag"]) == (5404, 10154)
    assert (r["av_an"], r["av_ag"]) == (755, 1419)


def test_uebergangsbereich_bsp9_sachsen():
    # Rundschreiben Bsp. 9: 1.100,00 €, Sachsen, kinderlos: BE 1.002,67 €, BE_AN 783,78 €
    # Beitrag 30,58 €, AN-Anteil 783,78 € · 2,025 % = 15,87 €, AG 14,71 €, Zuschlag 1.002,67 · 0,35 % = 3,51 €
    r = _rechne(_w2023(), entgelt=110000, sachsen=True, pv_kinderlos=True)
    assert (r["bemessung_kv"], r["bemessung_an_kv"]) == (100267, 78378)
    assert r["pv_ag"] == 1471
    assert r["pv_an"] == 1587 + 351


def test_uebergangsbereich_2026_beispiel_lohn_info():
    # lohn-info.de/uebergangsbereich_gleitzone_2026.html (nach dem Rundschreiben gerechnet): Mai 2026, 1.000,00 €,
    # Zusatzbeitrag 3,0 %, 1 Kind (kein Abschlag), nicht Sachsen. BE 854,06 €, BE_AN 568,36 €.
    r = beitraege(2026, entgelt=100000, kv_zusatz=D("3.0"), pv_kinder=1)
    assert (r["bemessung_kv"], r["bemessung_an_kv"]) == (85406, 56836)
    assert (r["kv_an"], r["kv_ag"]) == (5002, 10030)
    assert (r["pv_an"], r["pv_ag"]) == (1023, 2051)
    assert (r["rv_an"], r["rv_ag"]) == (5286, 10600)
    assert (r["av_an"], r["av_ag"]) == (739, 1481)
    assert r["an_summe"] == 12050                                    # 120,50 €
    assert r["ag_summe"] - r["insolvenz"] == 24162                   # 241,62 €


def test_uebergangsbereich_arbeitgeber_zahlt_differenz_gesamt_minus_an():
    # Gesamtbeitrag = 2 · gerundeter halber Satz auf BE; AG = Gesamt − AN (BVV § 2 Abs. 2 Satz 1 und 4)
    r = beitraege(2026, entgelt=100000)
    be, ban = r["bemessung_kv"], r["bemessung_an_kv"]
    from kontor.core.money import percent_of
    gesamt = 2 * percent_of(be, D("9.3"))
    assert r["rv_an"] + r["rv_ag"] == gesamt
    assert r["rv_an"] == percent_of(ban, D("9.3"))


def test_uebergangsbereich_pv_abschlag_auf_be_an():
    # BVV § 2 Abs. 2 Satz 7: Abschlag auf BE_AN (Satz 6), nicht auf BE. 3 Kinder = 0,5 Punkte
    # 2026, 1.000,00 €: BE_AN 568,36 € → Abschlag 2,84 (2,8418)
    ohne = beitraege(2026, entgelt=100000, pv_kinder=1)
    mit = beitraege(2026, entgelt=100000, pv_kinder=3)
    assert ohne["pv_an"] - mit["pv_an"] == 284
    assert ohne["pv_ag"] == mit["pv_ag"]


def test_uebergangsbereich_grenzen():
    g, og = 60300, 200000                                            # 2026
    assert beitraege(2026, entgelt=g)["art"] == "regulaer"           # Minijob-Grenze selbst ist kein Midijob (bis 603,00 €)
    assert beitraege(2026, entgelt=g + 1)["art"] == "midijob"        # 603,01 €: BE_AN = 0,01 € (lohn-info Tabelle)
    assert beitraege(2026, entgelt=g + 1)["bemessung_an_kv"] == 1
    r = beitraege(2026, entgelt=og)
    assert (r["art"], r["bemessung_kv"], r["bemessung_an_kv"]) == ("midijob", og, og)   # an der Obergrenze BE = AE
    assert beitraege(2026, entgelt=og + 1)["art"] == "regulaer"


def test_uebergangsbereich_bsp17_unter_der_grenze_traegt_arbeitgeber_allein():
    # Rundschreiben Bsp. 17/18: Entgelt unter 520,00 € in einem Übergangsbereichs-Beschäftigungsverhältnis:
    # BE = F · AE = 0,6922 · 500,00 = 346,10 €; Arbeitgeber trägt allein, Beschäftigter nur den Kinderlosenzuschlag.
    r = _rechne(_w2023(), entgelt=50000, uebergangsbereich=True, pv_kinderlos=True)
    assert r["bemessung_kv"] == 34610 and r["bemessung_an_kv"] == 0
    assert (r["kv_an"], r["rv_an"], r["av_an"]) == (0, 0, 0)
    assert r["pv_an"] == 121                                         # 346,10 · 0,35 % = 1,21
    assert r["rv_ag"] == 2 * 3219                                    # 346,10 · 9,3 % = 32,19 → 64,38


def test_uebergangsbereich_teilmonat_bsp6_und_7a():
    # Rundschreiben Bsp. 6: 650,00 € monatlich, Ende am 12.11.: Arbeitsentgelt 260,00 €, 12 SV-Tage
    # BE(650) = 504,00; BE_AN(650) = 175,68; anteilig x 12/30: 201,60 und 70,27
    w = _w2023()
    r = _rechne(w, entgelt=26000, sv_tage=12)
    assert (r["bemessung_kv"], r["bemessung_an_kv"]) == (20160, 7027)
    # Bsp. 7a: 800,00 € monatlich (Teilentgelt 320,00 €), Einmalzahlung 200,00 €: BE(1.000) = 891,85 → 356,74; BE_AN 648,65 → 259,46
    r = _rechne(w, entgelt=32000, einmal=20000, sv_tage=12)
    assert (r["bemessung_kv"], r["bemessung_an_kv"]) == (35674, 25946)


def test_uebergangsbereich_umlagen_bsp26_einmal():
    # Rundschreiben Bsp. 26: 900,00 € + Einmal 300,00 €. U1/U2 aus BE(900) = 781,04 (Bsp. 7b), ohne Einmal;
    # Insolvenzgeldumlage und Beiträge aus BE(1.200): 0,6922·520 + 1,108146·680 = 359,944 + 753,539 = 1.113,48
    r = _rechne(_w2023(), entgelt=90000, einmal=30000, u1=D("1.0"), u2=D("0.5"))
    assert r["bemessung_umlage"] == 78104 and r["bemessung_insolvenz"] == 111348
    assert (r["u1"], r["u2"]) == (781, 391)                          # 7,81 € und 3,90 € (3,9052)
    assert r["insolvenz"] == 167                                     # 1.113,48 · 0,15 % = 1,67


def test_uebergangsbereich_bsp27_einmal_ueber_der_obergrenze():
    # Rundschreiben Bsp. 27: 1.900,00 € + 300,00 € Einmal: Beiträge und IGU aus 2.200,00 €, U1/U2 nur aus 1.900,00 €
    r = _rechne(_w2023(), entgelt=190000, einmal=30000, u1=D("1.0"))
    assert r["art"] == "regulaer"
    assert r["bemessung_rv"] == 220000 and r["bemessung_insolvenz"] == 220000
    assert r["bemessung_umlage"] == 190000 and r["u1"] == 1900


def test_uebergangsbereich_nicht_fuer_azubis():
    # § 20 Abs. 2a Satz 8 SGB IV
    assert beitraege(2026, entgelt=100000, personengruppe="102")["art"] == "regulaer"
    with pytest.raises(KontorError, match="kennt keinen Übergangsbereich"):
        beitraege(2026, entgelt=100000, personengruppe="102", uebergangsbereich=True)


def test_uebergangsbereich_pkv_nicht_unterstuetzt():
    with pytest.raises(KontorError, match="Übergangsbereich"):
        beitraege(2026, entgelt=100000, beitragsgruppe="0110", pkv_beitrag=30000)


# ---------------------------------------------------------------- Minijob

def test_minijob_bsp10_rundschreiben():
    # Rundschreiben Bsp. 10 (510,00 €, gewerblich): RV-Beitrag 510 · 9,3 % · 2 = 94,86; AG 15 % = 76,50; AN 18,36
    # KV-Pauschale 13 % = 66,30
    r = _rechne(_w2023(), entgelt=51000, beitragsgruppe="6100", personengruppe="109")
    assert r["kv_pauschal"] == 6630
    assert r["rv_pauschal"] == 7650
    assert r["rv_an"] == 1836
    assert (r["kv_an"], r["kv_ag"], r["rv_ag"], r["pv_an"]) == (0, 0, 0, 0)
    assert r["art"] == "minijob" and r["pauschsteuer_basis"] == 51000


def test_minijob_2026_603_euro():
    # 603,00 €: KV 13 % = 78,39; RV 15 % = 90,45 → 90,45 (9045); Aufstockung: 18,6 % = 112,158 → 112,16 − 90,45 = 21,71
    # (= 3,6 % Eigenanteil, Minijob-Zentrale 2026). Umlagen der Minijob-Zentrale 2026: U1 0,8 % = 4,824 → 4,82;
    # U2 0,22 % = 1,3266 → 1,33; IGU 0,15 % = 0,9045 → 0,90. AG gesamt 175,89 €, mit 2 % Pauschsteuer (12,06) 187,95 € = 31,17 %
    w = werte(2026)
    u1, u2 = D(w["umlage_u1_minijob"]["wert"]), D(w["umlage_u2_minijob"]["wert"])
    r = beitraege(2026, entgelt=60300, beitragsgruppe="6100", personengruppe="109", u1=u1, u2=u2)
    assert (r["kv_pauschal"], r["rv_pauschal"], r["rv_an"]) == (7839, 9045, 2171)
    assert (r["u1"], r["u2"], r["insolvenz"]) == (482, 133, 90)
    assert r["ag_summe"] == 7839 + 9045 + 482 + 133 + 90 == 17589
    assert r["an_summe"] == 2171
    assert r["ag_summe"] + round(60300 * 0.02) == 18795


def test_minijob_mindestbemessungsgrundlage_175_euro():
    # § 163 Abs. 8 SGB VI: unter 175 € gilt 175 €. 100,00 €: Beitrag 18,6 % von 175,00 € = 32,55; AG 15 % von 100,00 € = 15,00
    # → AN 17,55 € (BVV § 2 Abs. 1 Satz 5)
    r = beitraege(2025, entgelt=10000, beitragsgruppe="6100", personengruppe="109")
    assert (r["rv_pauschal"], r["rv_an"]) == (1500, 1755)
    assert r["bemessung_rv"] == 17500


def test_minijob_befreit_von_rv_nur_arbeitgeber_pauschal():
    r = beitraege(2025, entgelt=50000, beitragsgruppe="6500", personengruppe="109")
    assert (r["rv_pauschal"], r["rv_an"]) == (7500, 0)
    r = beitraege(2025, entgelt=50000, beitragsgruppe="6100", personengruppe="109", rv_aufstockung=False)
    assert (r["rv_pauschal"], r["rv_an"]) == (7500, 0)


def test_minijob_privat_versichert_keine_kv_pauschale():
    # KV-Ziffer 0: § 249b SGB V gilt nur für gesetzlich Versicherte
    r = beitraege(2025, entgelt=50000, beitragsgruppe="0500", personengruppe="109")
    assert r["kv_pauschal"] == 0 and r["rv_pauschal"] == 7500


def test_minijob_umlagen_mit_einmal_nur_insolvenz():
    # Einmal-Entgelt zählt nicht für U1/U2, aber für die Insolvenzgeldumlage (wie bei Beschäftigten, § 7 AAG, § 358 SGB III)
    r = beitraege(2025, entgelt=40000, einmal=10000, beitragsgruppe="6500", personengruppe="109", u1=D("1.1"), u2=D("0.22"))
    assert r["u1"] == 440 and r["u2"] == 88                          # 400,00 · 1,1 % = 4,40; · 0,22 % = 0,88
    assert r["insolvenz"] == 75                                      # 500,00 · 0,15 % = 0,75
    assert r["kv_pauschal"] == 6500                                  # 13 % von 500,00 €


def test_minijob_ungueltige_beitragsgruppe():
    with pytest.raises(KontorError, match="Minijob"):
        beitraege(2025, entgelt=50000, beitragsgruppe="1111", personengruppe="109")
    with pytest.raises(KontorError, match="Pauschalbeitrag"):
        beitraege(2025, entgelt=50000, beitragsgruppe="6500", personengruppe="101")


def test_kurzfristig_nur_umlagen_und_keine_sv():
    r = beitraege(2025, entgelt=100000, beitragsgruppe="0000", personengruppe="110", u2=D("0.22"))
    assert (r["art"], r["an_summe"], r["kv_pauschal"], r["rv_pauschal"]) == ("keine", 0, 0, 0)
    assert (r["u2"], r["insolvenz"]) == (220, 150)                   # 1.000,00 € · 0,22 % = 2,20; 0,15 % = 1,50
    with pytest.raises(KontorError):
        beitraege(2025, entgelt=100000, beitragsgruppe="1111", personengruppe="110")


def test_zwischenpraktikum_alles_null():
    r = beitraege(2025, entgelt=100000, beitragsgruppe="0000", personengruppe="190", u1=D("1.1"))
    assert r["art"] == "keine" and r["ag_summe"] == r["an_summe"] == r["insolvenz"] == r["u1"] == 0


# ---------------------------------------------------------------- Auszubildende

def test_azubi_bis_325_euro_arbeitgeber_allein():
    # § 20 Abs. 3 SGB IV: 300,00 €, kinderlos: KV 7,3 % = 21,90 → AG 43,80; RV 27,90 → 55,80; AV 3,90 → 7,80;
    # PV 5,40 → AG 10,80; Kinderlosenzuschlag 1,80 € bleibt beim Azubi
    r = beitraege(2025, entgelt=30000, personengruppe="102", pv_kinderlos=True)
    assert (r["kv_an"], r["kv_ag"]) == (0, 4380)
    assert (r["rv_an"], r["rv_ag"]) == (0, 5580)
    assert (r["av_an"], r["av_ag"]) == (0, 780)
    assert (r["pv_an"], r["pv_ag"]) == (180, 1080)


def test_azubi_ueber_325_euro_hälftig():
    r = beitraege(2025, entgelt=40000, personengruppe="102")
    assert r["rv_an"] == r["rv_ag"] == 3720                          # 400,00 · 9,3 % = 37,20


def test_azubi_einmal_ueber_die_grenze_nicht_unterstuetzt():
    with pytest.raises(KontorError, match="Geringverdienergrenze"):
        beitraege(2025, entgelt=30000, einmal=30000, personengruppe="102")


# ---------------------------------------------------------------- Zuschuss zur privaten KV/PV

def test_pkv_zuschuss_hoechstens_haelfte_und_hoechstens_ag_anteil():
    # § 257 Abs. 2 SGB V, 2025: Höchstbetrag = 5.512,50 € · (14,6 % + 2,5 %) / 2 = 471,32 € (471,31875)
    kw = dict(entgelt=600000, beitragsgruppe="0110")
    r = beitraege(2025, pkv_beitrag=80000, **kw)                     # Hälfte 400,00 € < 471,32 €
    assert r["pkv_zuschuss"] == 40000
    r = beitraege(2025, pkv_beitrag=120000, **kw)                    # Hälfte 600,00 € > 471,32 € → gedeckelt
    assert r["pkv_zuschuss"] == 47132
    r = beitraege(2025, pkv_beitrag=80001, **kw)                     # ungerade Cent: abrunden, die Hälfte nie überschreiten
    assert r["pkv_zuschuss"] == 40000
    assert r["kv_an"] == r["kv_ag"] == 0 and r["rv_ag"] == 55800    # RV 9,3 % von 6.000,00 € (unter der RV-BBG) = 558,00
    assert r["ag_summe"] == r["rv_ag"] + r["av_ag"] + r["insolvenz"]     # Zuschuss gehört nicht zu ag_summe


def test_pv_zuschuss_und_sachsen():
    # § 61 Abs. 2 SGB XI: höchstens AG-Anteil: 5.512,50 € · 1,8 % = 99,225 → 99,23; Sachsen 1,3 % = 71,6625 → 71,66
    kw = dict(entgelt=600000, beitragsgruppe="0110", pkv_pv_beitrag=30000)
    assert beitraege(2025, **kw)["pv_zuschuss"] == 9923
    assert beitraege(2025, sachsen=True, **kw)["pv_zuschuss"] == 7166
    assert beitraege(2025, **{**kw, "pkv_pv_beitrag": 10000})["pv_zuschuss"] == 5000   # Hälfte des Beitrags


def test_pkv_zuschuss_nur_ohne_gesetzliche_versicherung():
    with pytest.raises(KontorError, match="KV 0"):
        beitraege(2025, entgelt=600000, beitragsgruppe="1111", pkv_beitrag=30000)
    with pytest.raises(KontorError, match="PV 0"):
        beitraege(2025, entgelt=600000, beitragsgruppe="0111", pkv_pv_beitrag=30000)


# ---------------------------------------------------------------- Eingaben

@pytest.mark.parametrize("kwargs,text", [
    (dict(entgelt=-1), "negativ"),
    (dict(entgelt=100.5), "ganze Zahl"),
    (dict(entgelt=100, sv_tage=0), "sv_tage"),
    (dict(entgelt=100, sv_tage=31), "sv_tage"),
    (dict(entgelt=100, beitragsgruppe="111"), "vier Ziffern"),
    (dict(entgelt=100, beitragsgruppe="2111"), "Ziffer 2 für KV"),
    (dict(entgelt=100, beitragsgruppe="1411"), "Ziffer 4 für RV"),
    (dict(entgelt=100, personengruppe="103"), "nicht unterstützt"),
    (dict(entgelt=100, kv_zusatz=2.5), "Decimal"),
    (dict(entgelt=100, kv_zusatz=D("-1")), "Decimal"),
    (dict(entgelt=100, bisher_entgelt=500), "bisher_sv_tage"),
    (dict(entgelt=100, monat=13), "monat"),
])
def test_ungueltige_eingaben(kwargs, text):
    with pytest.raises(KontorError, match=text):
        beitraege(2025, **kwargs)


def test_unbekanntes_jahr_in_beitraege():
    with pytest.raises(KontorError, match="2031"):
        beitraege(2031, entgelt=100000)


def test_alle_ausgabefelder_sind_ganze_cent():
    r = beitraege(2026, entgelt=123457, einmal=1111, kv_zusatz=D("2.9"), pv_kinder=2, u1=D("0.8"), u2=D("0.22"))
    for k, v in r.items():
        if k != "art":
            assert isinstance(v, int) and not isinstance(v, bool), k
    assert r["an_summe"] == r["kv_an"] + r["rv_an"] + r["av_an"] + r["pv_an"]
