"""Umsatzsteuer-Voranmeldung als ELSTER-Datensatz. Alle Daten erfunden; Steuernummer aus den ERiC-Beispielen
(Testfinanzamt 9198). Die ERiC-Prüfung läuft nur mit `KONTOR_ERIC` und wird sonst übersprungen; geprüft wird lokal
(VALIDIERE), es geht nichts an ELSTER."""
from datetime import date
from xml.etree import ElementTree as ET

import pytest

from kontor.core.errors import KontorError
from kontor.elster import eric
from kontor.elster import ustva as uv
from kontor.elster.lohn import Arbeitgeber

U = Arbeitgeber("Erika Muster", "Musterweg", "12a", "80333", "München", "9198011310134", "089 1234567",
                "info@example.org")
HID = "00000"


def satz(kz=None, **kw):
    kw.setdefault("erstellt", date(2026, 11, 2))
    return uv.UStVA(2026, kw.pop("zeitraum", "09"), {"81": 1000, "66": 3800} if kz is None else kz, **kw)


def xml(a=None, test=True):
    return uv.ustva(a or satz(), U, HID, test, "Erika Muster")


def werte(text):
    root = ET.fromstring(text.encode("utf-8"))
    vor = next(e for e in root.iter() if e.tag.endswith("}Umsatzsteuervoranmeldung"))
    return [(e.tag.split("}")[1], e.text) for e in vor]


def test_datenart_und_zeitraum():
    assert uv.datenart(2026) == "UStVA_2026"
    with pytest.raises(KontorError, match="Schema"):
        uv.datenart(2019)
    assert uv.zeitraum("2026-09") == (2026, "09") and uv.zeitraum("2026-Q3") == (2026, "43")
    for falsch in ("2026", "2026-13", "2026-Q5", "September"):
        with pytest.raises(KontorError):
            uv.zeitraum(falsch)


def test_felder_in_schema_reihenfolge_und_formaten():
    a = satz({"86": 100, "81": 1000, "66": 3800, "84": 9, "85": 188, "67": 188, "46": 13, "47": 247})
    assert werte(xml(a)) == [("Jahr", "2026"), ("Zeitraum", "09"), ("Steuernummer", "9198011310134"), ("Kz09", HID),
                             ("Kz46", "13"), ("Kz47", "2.47"), ("Kz66", "38.00"), ("Kz67", "1.88"), ("Kz81", "1000"),
                             ("Kz83", "161.47"), ("Kz84", "9"), ("Kz85", "1.88"), ("Kz86", "100")]


def test_kz83_rechnet_wie_elster():
    """Steuer zu Kz81/86 aus ganzen Euro, Reverse Charge hebt sich mit Kz67 auf, Überschuss ist negativ."""
    assert satz({"81": 320}).kz83 == 6080
    assert satz({"84": 9, "85": 188, "67": 188}).kz83 == 0
    assert satz({"66": 1234}).kz83 == -1234
    assert "<Kz83>-12.34</Kz83>" in xml(satz({"66": 1234}))


def test_nullmeldung_hat_kz83():
    assert werte(xml(satz({})))[-1] == ("Kz83", "0.00")


def test_berichtigt_widnr_und_testmerker():
    a = xml(satz(berichtigt=True, widnr="DE123456789"))
    assert ("Kz10", "1") in werte(a) and ("WIdNr", "DE123456789") in werte(a)
    assert "<Testmerker>700000004</Testmerker>" in a and "<Testmerker>" not in xml(test=False)
    assert "ustva/v2026" in a and "<DatenArt>UStVA</DatenArt>" in a


def test_eingaben_werden_geprueft():
    with pytest.raises(KontorError, match="nicht abgebildet"):
        xml(satz({"99": 1}))
    with pytest.raises(KontorError, match="negativ"):
        xml(satz({"84": -5}))
    with pytest.raises(KontorError, match="Wirtschafts"):
        xml(satz(widnr="123"))
    with pytest.raises(KontorError, match="Zeitraum"):
        xml(satz(zeitraum="19"))


def _eric_da():
    try:
        eric.eric_dir()
    except KontorError:
        return False
    return True


@pytest.mark.skipif(not _eric_da(), reason="KONTOR_ERIC nicht gesetzt oder ERiC nicht vorhanden")
class TestEric:
    def _ok(self, r):
        assert r["ok"] or r["code"] == 610001003, (r["text"], r["meldungen"])

    def test_monat_mit_reverse_charge(self):
        a = satz({"81": 320, "84": 9, "85": 188, "67": 188})
        self._ok(eric.pruefen(xml(a), uv.datenart(2026)))

    def test_quartal_mit_vorsteuerueberschuss_und_berichtigung(self):
        self._ok(eric.pruefen(xml(satz({"66": 1234}, zeitraum="43", berichtigt=True)), uv.datenart(2026)))

    def test_nullmeldung(self):
        self._ok(eric.pruefen(xml(satz({})), uv.datenart(2026)))

    def test_falsche_kz83_wird_abgelehnt(self):
        falsch = xml().replace("<Kz83>152.00</Kz83>", "<Kz83>1.00</Kz83>")
        assert "<Kz83>1.00</Kz83>" in falsch
        assert not eric.pruefen(falsch, uv.datenart(2026))["ok"]
