"""Lohnsteuer-Kern: der PAP-Interpreter gegen Referenzwerte der BMF-Schnittstelle (tests/fixtures/lohnsteuer_bmf.json,
erfundene Eingaben, Ausgaben vom BMF-Rechner) und die BigDecimal-Regeln im Einzelnen."""
import json
from decimal import Decimal
from pathlib import Path

import pytest

from kontor.core import lohnsteuer as L
from kontor.core.errors import KontorError

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "lohnsteuer_bmf.json").read_text(encoding="utf-8"))
FAELLE = [(int(jahr), i, f) for jahr, liste in sorted(FIXTURE["faelle"].items()) for i, f in enumerate(liste)]


def test_fixture_deckt_beide_jahre_ab():
    for jahr in L.jahre():
        assert len(FIXTURE["faelle"][str(jahr)]) >= 60


@pytest.mark.parametrize("jahr,i,fall", FAELLE, ids=[f"{j}-{i}" for j, i, _ in FAELLE])
def test_gegen_bmf(jahr, i, fall):
    ist = L.berechne(jahr, fall["ein"])
    soll = {k: Decimal(v) for k, v in fall["aus"].items()}
    assert ist.keys() == soll.keys()
    assert ist == soll, fall["ein"]


def test_jahre_und_eingaben():
    assert L.jahre() == [2025, 2026]
    assert L.eingaben(2026)["RE4"] == "BigDecimal"
    assert L.eingaben(2026)["STKL"] == "int"
    assert "ALV" in L.eingaben(2026) and "ALV" not in L.eingaben(2025)
    assert "PKPVAGZ" in L.eingaben(2026) and "PKPVAGZ" not in L.eingaben(2025)


def test_fehlendes_jahr():
    with pytest.raises(KontorError, match="Für 2031 liegt kein Programmablaufplan vor"):
        L.berechne(2031, {})


def test_unbekannte_eingabe():
    with pytest.raises(KontorError, match="unbekannte Eingabe NETTO"):
        L.berechne(2026, {"NETTO": 1})
    with pytest.raises(KontorError, match="unbekannte Eingabe ALV"):
        L.berechne(2025, {"LZZ": 1, "STKL": 1, "ALV": 1})


def test_vorgabewerte_und_pflichtfelder():
    # 2026: LZZ=1 und STKL=1 sind im PAP vorbelegt; 2025 hat keine Vorgabe
    assert L.berechne(2026, {"RE4": 2500000}) == L.berechne(2026, {"RE4": 2500000, "LZZ": 1, "STKL": 1})
    with pytest.raises(KontorError, match="LZZ fehlt"):
        L.berechne(2025, {"STKL": 1, "RE4": 2500000})
    with pytest.raises(KontorError, match="Steuerklasse 7"):
        L.berechne(2026, {"STKL": 7})
    with pytest.raises(KontorError, match="Lohnzahlungszeitraum 5"):
        L.berechne(2026, {"LZZ": 5})


def test_eingabetypen():
    a = L.berechne(2026, {"LZZ": 2, "STKL": 1, "RE4": "450000", "ZKF": "0.5"})
    assert a == L.berechne(2026, {"LZZ": 2, "STKL": 1, "RE4": Decimal(450000), "ZKF": Decimal("0.5")})
    assert a == L.berechne(2026, {"LZZ": 2, "STKL": 1, "RE4": 450000, "ZKF": 0.5})
    with pytest.raises(KontorError, match="ganze Zahl"):
        L.berechne(2026, {"STKL": 1.5})
    with pytest.raises(KontorError, match="keine Zahl"):
        L.berechne(2026, {"RE4": "viel"})
    with pytest.raises(KontorError, match="keine Zahl"):
        L.berechne(2026, {"RE4": float("nan")})


def test_bmf_beispiel_und_hauptausgaben():
    # Beispiel der BMF-Seite zur externen Schnittstelle: Jahr, 25.000 €, Steuerklasse 1
    assert L.lohnsteuer(2026, LZZ=1, RE4=2500000, STKL=1) == {
        "lst": 137600, "soli": 0, "kist_basis": 0, "lst_sonst": 0, "soli_sonst": 0,
        "kist_basis_sonst": 0, "vkv": 0, "vkv_sonst": 0}
    r = L.lohnsteuer(2026, LZZ=2, RE4=900000, STKL=1, R=1, JRE4=10800000, SONSTB=1000000)
    assert r["lst"] > 0 and r["kist_basis"] > 0
    assert r["lst_sonst"] > 0 and r["kist_basis_sonst"] == r["lst_sonst"]
    # PAP 2025 kennt VKVLZZ (private Basisabsicherung), 2026 nicht mehr
    assert L.lohnsteuer(2025, LZZ=2, RE4=400000, STKL=1, PKV=2, PKPV=30000)["vkv"] == 15833
    assert L.lohnsteuer(2026, LZZ=2, RE4=400000, STKL=1, PKV=1, PKPV=30000)["vkv"] == 0


def test_kirchensteuer_basis_nur_mit_religion():
    ohne = L.lohnsteuer(2026, LZZ=2, RE4=800000, STKL=1, R=0)
    mit = L.lohnsteuer(2026, LZZ=2, RE4=800000, STKL=1, R=1)
    assert ohne["kist_basis"] == 0 and mit["kist_basis"] > 0 and ohne["lst"] == mit["lst"]


# ---- BigDecimal-Regeln --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("wert,modus,soll", [
    ("2.5", "DOWN", "2"), ("2.5", "UP", "3"), ("-2.5", "DOWN", "-2"), ("-2.5", "UP", "-3"),
    ("2.5", "HALF_UP", "3"), ("2.5", "HALF_EVEN", "2"), ("3.5", "HALF_EVEN", "4"), ("-2.5", "HALF_UP", "-3"),
    ("-2.5", "FLOOR", "-3"), ("-2.5", "CEILING", "-2"), ("2.4", "UP", "3"), ("2.0", "UP", "2"),
])
def test_setscale_wie_java(wert, modus, soll):
    assert L._setscale(Decimal(wert), 0, modus) == Decimal(soll)


@pytest.mark.parametrize("a,b,skala,modus,soll", [
    ("10", "3", 2, "DOWN", "3.33"), ("10", "3", 2, "UP", "3.34"), ("-10", "3", 2, "DOWN", "-3.33"),
    ("-10", "3", 2, "UP", "-3.34"), ("-10", "3", 2, "FLOOR", "-3.34"), ("-10", "3", 2, "CEILING", "-3.33"),
    ("5", "2", 0, "HALF_UP", "3"), ("5", "2", 0, "HALF_EVEN", "2"), ("-5", "2", 0, "HALF_DOWN", "-2"),
    ("7", "12", 0, "DOWN", "0"), ("123456", "100", 2, "DOWN", "1234.56"),
])
def test_divide_mit_skala_wie_java(a, b, skala, modus, soll):
    assert L._divs(Decimal(a), Decimal(b), skala, modus) == Decimal(soll)


def test_divide_ohne_skala_ist_exakt_oder_fehler():
    assert L._divx(Decimal(2880), Decimal(12)) == Decimal(240)
    with pytest.raises(ArithmeticError):          # Java: „Non-terminating decimal expansion“
        L._divx(Decimal(1), Decimal(3))


def test_keine_negative_null():
    assert str(L._out(L._setscale(Decimal("-0.4"), 0, "DOWN"))) == "0"


# ---- Whitelist des Interpreters -----------------------------------------------------------------------------------

def _ue():
    return L._Uebersetzer({"A": "BigDecimal", "N": "int"}, {"TAB": 1})


def test_fremde_ausdruecke_abgelehnt():
    for text in ("__import__('os')", "A.getClass()", "Runtime.getRuntime()", "B.add(A)", "A.divide(A, 2, 3)",
                 "A.setScale(N, BigDecimal.ROUND_DOWN)", "A.pow(2)", "N / 2", "A.add(A", "new BigDecimal(0.1)",
                 "BigDecimal.valueOf(A).abs()", "N[1]", "A; B"):
        with pytest.raises(KontorError):
            p = L._Parser(text)
            _ue().e(p.ausdruck())
            p.fertig()


def test_zulaessige_ausdruecke_uebersetzt():
    for text in ("A.add(BigDecimal.valueOf(0.5)).setScale(2, BigDecimal.ROUND_UP)",
                 "A.divide(BigDecimal.ONE, 2, BigDecimal.ROUND_DOWN)", "N < 5 && A.compareTo(BigDecimal.ZERO) == 1",
                 "new BigDecimal(7)", "BigDecimal.valueOf(N - 2004)", "TAB[N]"):
        p = L._Parser(text)
        assert _ue().e(p.ausdruck())
        p.fertig()


def test_pap_mit_unbekanntem_bezeichner_wird_nicht_geladen(tmp_path, monkeypatch):
    xml = (L.PAP_DIR / "Lohnsteuer2026.xml").read_bytes().replace(b'exec="GFB = BigDecimal.valueOf(12348)"',
                                                                  b'exec="GFB = Runtime.valueOf(12348)"')
    with pytest.raises(KontorError, match="Runtime"):
        L._Pap(2026, xml)
