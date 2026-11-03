"""Lohn-Datensätze für ELSTER (LStA, LStB). Alle Daten erfunden; Steuernummer und Test-IdNr. aus den ERiC-Beispielen
(Testfinanzamt 9198). Die ERiC-Prüfung läuft nur mit `KONTOR_ERIC` und wird sonst übersprungen; geprüft wird lokal
(VALIDIERE), es geht nichts an ELSTER."""
import os
import re
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from kontor.core.errors import KontorError
from kontor.elster import eric
from kontor.elster import lohn as lo

AG = lo.Arbeitgeber("Muster GmbH", "Musterweg", "12a", "80333", "München", "9198011310134",
                    "089 1234567", "lohn@example.org", "Testfinanzamt")
HID = "00000"         # Platzhalter; die Beispiel-ID 74931 lehnt ERiC 43.5 als gesperrt ab
LIEFERANT = "Muster GmbH; Musterweg 12a; 80333 München"


def lsta(jahr=2026, zeitraum="09", **kw):
    kw.setdefault("arbeitnehmer", 2)
    kw.setdefault("kz42", 30000)
    kw.setdefault("kz49", 1650)
    kw.setdefault("kz61", 2400)
    kw.setdefault("kz62", 600)
    return lo.LStA(jahr, zeitraum, erstellt=date(2026, 10, 5), **kw)


def lstb(**kw):
    base = dict(
        jahr=2025, dienst_von=date(2025, 1, 1), dienst_bis=date(2025, 12, 31), idnr="01127645382",
        ordnungsmerkmal="P-0007", name="Mustermann", vorname="Erika", geburtsdatum=date(1990, 5, 17),
        strasse="Beispielgasse", hausnummer="3", plz="80333", ort="München",
        merkmale=[lo.Besteuerung(date(2025, 1, 1), 1, kinder="0.0", konfession="ev"),
                  lo.Besteuerung(date(2025, 7, 1), 4, faktor="0.789", kinder="1.0", konfession="rk", ehegkonfession="ev",
                                 freibetrag=lo.Freibetrag(120000, 10000, 1665, 333))],
        brutto=4500000, lst=420000, soli=0, kist_an=33600, ag_rv=418500, an_rv=418500, an_kv=366000, an_pv=76500,
        an_av=58500, zuschuss_gkv=300000, grossbuchstaben="S,M", kammerbeitrag={"HB": 12000})
    base.update(kw)
    return lo.LStB(**base)


def lsta_xml(**kw):
    return lo.lsta(lsta(**kw.pop("a", {})), AG, HID, True, LIEFERANT, **kw)


def ns(xml):
    return ET.fromstring(xml.encode("utf-8"))


def kz(root, name):
    el = next((e for e in root.iter() if e.tag.endswith("}" + name)), None)
    return None if el is None else el.text


class TestLStA:
    def test_datenart(self):
        assert lo.lsta_datenart(2025) == "LStA_2025"
        assert lo.lsta_datenart(2026) == "LStA_2026"
        with pytest.raises(KontorError):
            lo.lsta_datenart(2027)

    def test_structure_and_sums(self):
        r = ns(lsta_xml())
        assert kz(r, "Verfahren") == "ElsterAnmeldung" and kz(r, "DatenArt") == "LStA"
        assert kz(r, "Testmerker") == "700000004"
        assert next(e for e in r.iter() if e.tag.endswith("}Empfaenger")).text == "9198"
        assert kz(r, "Steuernummer") == "9198011310134" and kz(r, "Kz09") == HID
        assert kz(r, "Kz42") == "300.00" and kz(r, "Kz42_lfdJahr") == "300.00"
        assert kz(r, "Kz48") == "300.00"                            # Kz42 ohne pauschale und Abzüge
        assert kz(r, "Kz49") == "16.50" and kz(r, "Kz61") == "24.00" and kz(r, "Kz62") == "6.00"
        assert kz(r, "Kz83") == "346.50"                            # 300 + 16,50 + 24 + 6
        assert kz(r, "Kz86") == "2"
        assert kz(r, "Kz10") is None and kz(r, "Kz63") is None      # null bleibt weg
        assert kz(r, "Hausnummer") == "12" and kz(r, "HNrZusatz") == "a"
        assert kz(r, "Zeitraum") == "09" and kz(r, "Erstellungsdatum") == "20261005"

    def test_summenregeln_der_plausiprüfung(self):
        a = lsta(kz41=5000, kz44=2000, kz33=100, kz45=700, kz90=1, kz47=400, kz70=900, kz42=lo.Anteile(lfd=30000))
        assert a.kz48 == 5000 + 30000 + 2000 - 100 - 700
        assert a.kz83 == 400 + a.kz48 + 900 + 1650 + 2400 + 600
        r = ns(lo.lsta(a, AG, HID, True, LIEFERANT))
        assert kz(r, "Kz48") == f"{a.kz48 / 100:.2f}" and kz(r, "Kz83") == f"{a.kz83 / 100:.2f}"
        assert kz(r, "Kz90") == "1" and kz(r, "Kz45") == "7.00"

    def test_jahresanteile(self):
        r = ns(lsta_xml(a=dict(zeitraum="01", kz42=lo.Anteile(vorjahr=10000, lfd=20000))))
        assert (kz(r, "Kz42_Vorjahr"), kz(r, "Kz42_lfdJahr"), kz(r, "Kz42")) == ("100.00", "200.00", "300.00")
        with pytest.raises(KontorError, match="Vorjahr"):
            lsta_xml(a=dict(zeitraum="05", kz42=lo.Anteile(vorjahr=1, lfd=1)))
        with pytest.raises(KontorError, match="Folgejahr"):
            lsta_xml(a=dict(zeitraum="05", kz49=lo.Anteile(folgejahr=1)))

    def test_nullanmeldung_hat_die_pflichtfelder(self):
        r = ns(lsta_xml(a=dict(kz42=0, kz49=0, kz61=0, kz62=0, arbeitnehmer=0)))
        assert (kz(r, "Kz42"), kz(r, "Kz48"), kz(r, "Kz49"), kz(r, "Kz83"), kz(r, "Kz86")) == \
            ("0.00", "0.00", "0.00", "0.00", "0")
        assert kz(r, "Kz42_lfdJahr") is None

    def test_berichtigt_und_flags(self):
        r = ns(lsta_xml(a=dict(berichtigt=True, kz29=True, kz26=True, kz23="Nachzahlung Dezember")))
        assert kz(r, "Kz10") == "1" and kz(r, "Kz29") == "1" and kz(r, "Kz26") == "1"
        assert kz(r, "Kz23") == "1" and kz(r, "Kz23_Begruendung") == "Nachzahlung Dezember"

    def test_kz92_erst_ab_2026(self):
        assert kz(ns(lsta_xml(a=dict(kz92=True))), "Kz92") == "1"
        with pytest.raises(KontorError, match="Kz92"):
            lsta_xml(a=dict(jahr=2025, kz92=True))

    def test_namespace_folgt_dem_jahr(self):
        assert "lsta/v2025" in lsta_xml(a=dict(jahr=2025, zeitraum="12"))
        assert "lsta/v2026" in lsta_xml()

    def test_eingaben_werden_geprüft(self):
        with pytest.raises(KontorError, match="Zeitraum"):
            lsta_xml(a=dict(zeitraum="13"))
        with pytest.raises(KontorError, match="Kz45"):
            lsta_xml(a=dict(kz45=100))
        with pytest.raises(KontorError, match="Hersteller"):
            lo.lsta(lsta(), AG, "123", True, LIEFERANT)
        with pytest.raises(KontorError, match="Steuernummer"):
            lo.lsta(lsta(), lo.Arbeitgeber("X", "Y", "1", "80333", "M", "5214/123/45670"), HID, True, LIEFERANT)
        with pytest.raises(KontorError, match="Hausnummer"):
            lo.lsta(lsta(), lo.Arbeitgeber("X", "Y", "ohne", "80333", "M", AG.steuernummer13), HID, True, LIEFERANT)

    def test_ohne_testmerker(self):
        assert "<Testmerker>" not in lo.lsta(lsta(), AG, HID, False, LIEFERANT)


class TestLStB:
    def test_datenart(self):
        assert lo.lstb_datenart(2025) == "ElsterLohn"
        assert lo.lstb_datenart(2026) == "ElsterKMV"
        with pytest.raises(KontorError, match="Schema nicht hinterlegt"):
            lo.lstb_datenart(2027)

    def test_structure_and_values(self):
        r = ns(lo.lstb(lstb(), AG, HID, True, LIEFERANT))
        assert kz(r, "Verfahren") == "ElsterLohn" and kz(r, "DatenArt") == "LStB"
        assert kz(r, "Testmerker") == "220000000"
        assert [e.text for e in r.iter() if e.tag.endswith("}Empfaenger")][-1] == "BY"
        assert kz(r, "IdNr") == "01127645382" and kz(r, "Ordnungsmerkmal") == "P-0007"
        assert kz(r, "Name") == "Mustermann" and kz(r, "Geburtsdatum") == "19900517"
        assert (kz(r, "Anfang"), kz(r, "Ende")) == ("0101", "3112")
        assert kz(r, "BruttoArbLohn") == "45000.00" and kz(r, "LSteuer") == "4200.00" and kz(r, "Soli") == "0.00"
        assert kz(r, "ArbnKiSteuer") == "336.00" and kz(r, "EhegKiSteuer") is None
        assert kz(r, "ArbgAnteilRenVers") == "4185.00" and kz(r, "StFreiGeKrankVers") == "3000.00"
        assert kz(r, "Grossbuchstaben") == "S,M" and kz(r, "Kammerbeitrag") == "120.00"
        assert kz(r, "ArbGFANr") == "9198" and kz(r, "ArbGFAName") == "Testfinanzamt"
        el = [e for e in r.iter() if e.tag.endswith("}ELStAM")]
        assert [e.get("gueltig_ab") for e in el] == ["2025-01-01", "2025-07-01"]
        kist = [e for e in r.iter() if e.tag.endswith("}Kirchensteuerabzug")]
        assert kist[1].get("konfession") == "rk" and kist[1].get("ehegkonfession") == "ev"
        sk = [e for e in r.iter() if e.tag.endswith("}Steuerklasse")]
        assert sk[1].get("faktor") == "0.789" and sk[1].text == "4"
        fb = next(e for e in r.iter() if e.tag.endswith("}Steuerfreibetrag"))
        assert fb.attrib == {"jahr": "1200", "monat": "100", "woche": "16.65", "tag": "3.33"}

    def test_neu_hat_keine_referenz(self):
        xml = lo.lstb(lstb(), AG, HID, True, LIEFERANT)
        assert 'art="Neu"' in xml and "RefKmId" not in xml
        assert lo.km_id(lstb(), AG) == "2025-LST-01127645382-01013112-9198011310134-P-0007-NEU"

    def test_korrektur(self):
        b = lstb(ref_km_id=lo.km_id(lstb(), AG), korrektur_nr=2)
        r = ns(lo.lstb(b, AG, HID, True, LIEFERANT, korrektur=True))
        assert kz(r, "KmId").endswith("-KOR-002") and kz(r, "RefKmId") == lo.km_id(lstb(), AG)
        assert kz(r, "KorrekturGrund") == "Einfache Berichtigung"
        assert next(e for e in r.iter() if e.tag.endswith("}Anweisung")).get("art") == "Korrektur"
        with pytest.raises(KontorError, match="ref_km_id"):
            lo.lstb(lstb(), AG, HID, True, LIEFERANT, korrektur=True)

    def test_storno(self):
        b = lstb(ref_km_id=lo.km_id(lstb(), AG))
        r = ns(lo.lstb_storno(b, AG, HID, True, LIEFERANT))
        assert kz(r, "KmId").endswith("-STO") and kz(r, "RefKmId") == b.ref_km_id
        assert kz(r, "Zuflussjahr") == "2025" and kz(r, "ArbGStNr") == AG.steuernummer13
        assert next(e for e in r.iter() if e.tag.endswith("}Anweisung")).get("art") == "Storno"
        with pytest.raises(KontorError, match="ref_km_id"):
            lo.lstb_storno(lstb(), AG, HID, True, LIEFERANT)

    def test_eingaben_werden_geprüft(self):
        with pytest.raises(KontorError, match="IdNr"):
            lo.lstb(lstb(idnr="123"), AG, HID, True, LIEFERANT)
        with pytest.raises(KontorError, match="Faktor"):
            lo.lstb(lstb(merkmale=[lo.Besteuerung(date(2025, 1, 1), 1, faktor="0.5")]), AG, HID, True, LIEFERANT)
        with pytest.raises(KontorError, match="Kinderfreibeträge"):
            lo.lstb(lstb(merkmale=[lo.Besteuerung(date(2025, 1, 1), 1, kinder="0.3")]), AG, HID, True, LIEFERANT)
        with pytest.raises(KontorError, match="ELStAM"):
            lo.lstb(lstb(merkmale=[]), AG, HID, True, LIEFERANT)
        with pytest.raises(KontorError, match="Finanzamt"):
            lo.lstb(lstb(), lo.Arbeitgeber(**{**AG.__dict__, "finanzamt": ""}), HID, True, LIEFERANT)

    def test_reihenfolge_wie_im_schema(self):
        xml = lo.lstb(lstb(unterbrechungen=0, ermst_betrag=100000, arbeitstage=200, stfrei_fahrtk_ausw=5000,
                          werte={"Frei": 100}, texte={"Hinweis": "x"}), AG, HID, True, LIEFERANT)
        order = ["AnzahlU", "BruttoArbLohn", "LSteuer", "Soli", "ArbnKiSteuer", "ErmStBetrMKalJahr", "ArbgAnteilRenVers",
                 "ArbnAnteilKrankVers", "Grossbuchstaben", "Kammerbeitrag", "AnzahlArbTag", "StFreiFahrtKAusw", "Wert",
                 "Text", "Arbeitgeber"]
        pos = [xml.index(f"<{n}>") if f"<{n}>" in xml else xml.index(f"<{n} ") for n in order]
        assert pos == sorted(pos)


def lst26(**kw):
    kw.setdefault("jahr", 2026)
    kw.setdefault("dienst_von", date(2026, 1, 1))
    kw.setdefault("dienst_bis", date(2026, 9, 30))
    return lstb(**kw)


class TestLSTMitteilung:
    """Lohnjahr ab 2026: Verfahren ElsterKMV, Kontrollmitteilung 7 mit LST-Detailversion 2."""

    def test_structure_and_values(self):
        b = lst26(aktivrente=400000, geschlecht="W", merkmale=[
            lo.Besteuerung(date(2026, 1, 1), 1, konfession="ev", beitr_pkv=60000, beitr_pkv_basis=12000)])
        r = ns(lo.lstb(b, AG, HID, True, LIEFERANT))
        assert kz(r, "Verfahren") == "ElsterKMV" and kz(r, "DatenArt") == "LSTMitteilung"
        assert kz(r, "Testmerker") == "240000000"
        assert [e.text for e in r.iter() if e.tag.endswith("}Empfaenger")][-1] == "NW"
        assert next(e for e in r.iter() if e.tag.endswith("}Anweisung")).get("art") == "Neu"
        assert kz(r, "KmId") == "2026-LST-01127645382-01013009-9198011310134-P-0007-NEU"
        assert kz(r, "Zeitraum") is None            # Attribute von/bis
        z = next(e for e in r.iter() if e.tag.endswith("}Zeitraum"))
        assert (z.get("von"), z.get("bis")) == ("20260101", "20260930")
        pers = next(e for e in r.iter() if e.tag.endswith("}NatPers"))
        assert pers.get("idNr") == "01127645382" and pers.get("geschlecht") == "w"
        assert kz(r, "BruttoArbLohn") == "45000.00" and kz(r, "FAName") is None
        assert [e.text for e in r.iter() if e.tag.endswith("}VerfasserInfo")] == ["9198", "Testfinanzamt"]
        assert kz(r, "BeitrPkv") == "600.00" and kz(r, "BeitrPkvBasis") == "120.00"
        wert = next(e for e in r.iter() if e.tag.endswith("}Wert"))
        assert wert.get("name") == "SteuerfreibetragAktivrente" and wert.text == "4000.00"
        assert kz(r, "ArbGName") is None and kz(r, "BeitrPrKrankVers") is None

    def test_nr28_entfaellt(self):
        with pytest.raises(KontorError, match="Nr. 28"):
            lo.lstb(lst26(pkv_beitraege=100), AG, HID, True, LIEFERANT)

    def test_felder_nur_ab_2026(self):
        with pytest.raises(KontorError, match="2026"):
            lo.lstb(lstb(merkmale=[lo.Besteuerung(date(2025, 1, 1), 1, beitr_pkv=100)]), AG, HID, True, LIEFERANT)
        with pytest.raises(KontorError, match="Aktivrente"):
            lo.lstb(lstb(aktivrente=100), AG, HID, True, LIEFERANT)
        with pytest.raises(KontorError, match="Aktivrente"):
            lo.lstb(lst26(aktivrente=2500000), AG, HID, True, LIEFERANT)

    def test_korrektur_und_storno(self):
        b = lst26(ref_km_id=lo.km_id(lst26(), AG), korrektur_nr=2)
        r = ns(lo.lstb(b, AG, HID, True, LIEFERANT, korrektur=True))
        assert kz(r, "KmId").endswith("-KOR-002") and kz(r, "RefKmId") == b.ref_km_id
        r = ns(lo.lstb_storno(b, AG, HID, True, LIEFERANT))
        assert next(e for e in r.iter() if e.tag.endswith("}KMStorno")).get("kmArt") == "LST"
        assert kz(r, "KmId").endswith("-STO") and kz(r, "RefKmId") == b.ref_km_id
        with pytest.raises(KontorError, match="ref_km_id"):
            lo.lstb_storno(lst26(), AG, HID, True, LIEFERANT)

    def test_lst_mitteilung_nur_2026(self):
        with pytest.raises(KontorError, match="ab Lohnjahr 2026"):
            lo.lst_mitteilung(lstb(), AG, HID, True, LIEFERANT)


# -- ERiC (lokal, VALIDIERE) -------------------------------------------------------------------------------------

def _eric_da():
    try:
        eric.eric_dir()
    except KontorError:
        return False
    return True


def _docs() -> Path | None:
    """XSD-Schemata aus dem lokalen ERiC-Dokumentationspaket (Lizenz: liegt nicht im Repo)."""
    if not _eric_da():
        return None
    base = eric.eric_dir().parent / "Dokumentation" / "Schnittstellenbeschreibungen" / "Sonstige" / "ElsterLohn" / "Schema"
    return base if (base / "lstb202501.xsd").is_file() else None


@pytest.mark.skipif(not _eric_da(), reason="KONTOR_ERIC nicht gesetzt oder ERiC nicht vorhanden")
class TestEric:
    def _ok(self, ergebnis):
        # 610001003 heißt: plausibel, mit Hinweisen
        assert ergebnis["ok"] or ergebnis["code"] == 610001003, (ergebnis["text"], ergebnis["meldungen"], ergebnis.get("log", ""))

    def test_lsta_2025(self):
        a = lo.LStA(2025, "12", 2, kz42=lo.Anteile(lfd=25000, folgejahr=5000), kz49=1650, kz61=2400, kz62=600,
                    erstellt=date(2026, 1, 10))
        self._ok(eric.pruefen(lo.lsta(a, AG, HID, True, LIEFERANT), lo.lsta_datenart(2025)))

    def test_lsta_2026(self):
        self._ok(eric.pruefen(lsta_xml(), lo.lsta_datenart(2026)))

    def test_lsta_2026_januar_mit_vorjahr_und_abzuegen(self):
        a = lsta(zeitraum="01", kz42=lo.Anteile(vorjahr=10000, lfd=20000), kz41=5000, kz45=700, kz90=1, kz47=400)
        self._ok(eric.pruefen(lo.lsta(a, AG, HID, True, LIEFERANT), lo.lsta_datenart(2026)))

    def test_lsta_2026_nullanmeldung(self):
        a = lsta(kz42=0, kz49=0, kz61=0, kz62=0, arbeitnehmer=1)
        ergebnis = eric.pruefen(lo.lsta(a, AG, HID, True, LIEFERANT), lo.lsta_datenart(2026))
        assert ergebnis["ok"], (ergebnis["text"], ergebnis["meldungen"])

    def test_lsta_2026_ohne_arbeitnehmer_gibt_hinweis(self):
        a = lsta(kz42=0, kz49=0, kz61=0, kz62=0, arbeitnehmer=0)
        ergebnis = eric.pruefen(lo.lsta(a, AG, HID, True, LIEFERANT), lo.lsta_datenart(2026))
        assert ergebnis["code"] == 610001003 and any("Kz86" in m for m in ergebnis["meldungen"])

    def test_lsta_2026_berichtigt(self):
        self._ok(eric.pruefen(lsta_xml(a=dict(berichtigt=True)), lo.lsta_datenart(2026)))

    def test_lsta_fehler_wird_gemeldet(self):
        schlecht = lsta_xml().replace("<Kz83>346.50</Kz83>", "<Kz83>1.00</Kz83>")
        ergebnis = eric.pruefen(schlecht, lo.lsta_datenart(2026))
        assert not ergebnis["ok"]

    def test_lstb_2025_header(self):
        """ERiC prüft die Nutzdaten der LStB nicht, nur Rahmen und Datenart."""
        self._ok(eric.pruefen(lo.lstb(lstb(), AG, HID, True, LIEFERANT), lo.lstb_datenart(2025)))

    def test_lstmitteilung_2026_header(self):
        """Auch hier prüft ERiC die Nutzdaten nicht (Verfahren ElsterKMV)."""
        self._ok(eric.pruefen(lo.lstb(lst26(), AG, HID, True, LIEFERANT), lo.lstb_datenart(2026)))
        storno = lo.lstb_storno(lst26(ref_km_id="x-1"), AG, HID, True, LIEFERANT)
        self._ok(eric.pruefen(storno, lo.lstb_datenart(2026)))

    @pytest.mark.skipif(_docs() is None, reason="XSD-Schemata (ERiC-Dokumentationspaket) nicht vorhanden")
    def test_lstb_2025_gegen_das_schema(self, tmp_path):
        lxml = pytest.importorskip("lxml.etree")
        for f in ("allg000008.xsd", "lstb202501.xsd", "lstb202501_adapter.xsd", "lstb_storno_000002.xsd",
                  "lstb_storno_000002_adapter.xsd"):   # Kopie im Temp-Ordner
            text = (_docs() / f).read_text(encoding="utf-8").replace(r"[\P{Z}]", "[^ ]")   # libxml2 kennt \P{Z} nicht
            (tmp_path / f).write_text(text, encoding="utf-8")
        schema = lxml.XMLSchema(lxml.parse(str(tmp_path / "lstb202501_adapter.xsd")))
        storno = lxml.XMLSchema(lxml.parse(str(tmp_path / "lstb_storno_000002_adapter.xsd")))
        neu = lstb()
        kor = lstb(ref_km_id=lo.km_id(neu, AG))
        voll = lstb(unterbrechungen=1, versorgungsbezuege=[lo.VBez(2020, 100000, 90000, "01", "08", 5000)],
                    ermst_versorgung=[lo.JahrBetrag(2020, 5000)], ermst_betrag=2000, kurzarbeitergeld=1000,
                    leistungen_progvorbeh=1000, titel="Dr.", namensvorsatz="von", geburtsname="Beispiel",
                    geschlecht="W", arbeitstage=200, nicht_ermst_versorgung=[lo.JahrBetrag(2019, 1000)],
                    werte={"Frei": 100}, texte={"Hinweis": "x"}, an_wb_umlage=100, ag_zusatzvers=100,
                    an_zusatzvers=100, stfrei_fahrtk_ausw=100, pkv_beitraege=60000, dba_tuerkei=100)
        for xml, sch in ((lo.lstb(neu, AG, HID, True, LIEFERANT), schema),
                         (lo.lstb(kor, AG, HID, True, LIEFERANT, korrektur=True), schema),
                         (lo.lstb(voll, AG, HID, True, LIEFERANT), schema),
                         (lo.lstb_storno(kor, AG, HID, True, LIEFERANT), storno)):
            nutz = re.search(r"<Nutzdaten>\s*(.*?)\s*</Nutzdaten>", xml, re.S).group(1)
            doc = lxml.fromstring(nutz.encode("utf-8"))
            assert sch.validate(doc), sch.error_log


def _kmv_schema() -> Path | None:
    """Ordner mit den KMV-Schemata (kmv000007.xsd, kmvstorno000007.xsd, kmv_LST_000002.xsd ...), öffentlich auf
    esteuer.de (KM7_LST_000002_Schema.zip); `KONTOR_LST_SCHEMA` zeigt darauf."""
    raw = os.environ.get("KONTOR_LST_SCHEMA", "")
    return Path(raw) if raw and (Path(raw) / "kmv000007.xsd").is_file() else None


@pytest.mark.skipif(_kmv_schema() is None, reason="KONTOR_LST_SCHEMA nicht gesetzt")
def test_lstmitteilung_2026_gegen_das_schema(tmp_path):
    lxml = pytest.importorskip("lxml.etree")
    for f in _kmv_schema().glob("*.xsd"):                              # Kopie im Temp-Ordner
        text = f.read_text(encoding="utf-8", errors="surrogateescape").replace(r"[\P{Z}]", "[^ ]")   # libxml2 kennt \P{Z} nicht
        (tmp_path / f.name).write_text(text, encoding="utf-8", errors="surrogateescape")
    schema = lxml.XMLSchema(lxml.parse(str(tmp_path / "kmv000007.xsd")))
    storno = lxml.XMLSchema(lxml.parse(str(tmp_path / "kmvstorno000007.xsd")))
    neu = lst26()
    voll = lst26(unterbrechungen=1, versorgungsbezuege=[lo.VBez(2020, 100000, 90000, "01", "08", 5000)],
                 ermst_versorgung=[lo.JahrBetrag(2020, 5000)], ermst_betrag=2000, kurzarbeitergeld=1000,
                 leistungen_progvorbeh=1000, titel="Dr.", namensvorsatz="von", namenszusatz="Graf",
                 geburtsname="Beispiel", geschlecht="W", arbeitstage=200, aktivrente=400000,
                 nicht_ermst_versorgung=[lo.JahrBetrag(2019, 1000)], werte={"Frei": 100}, texte={"Hinweis": "x"},
                 an_wb_umlage=100, ag_zusatzvers=100, an_zusatzvers=100, stfrei_fahrtk_ausw=100, dba_tuerkei=100,
                 hausnummer="3a", kammerbeitrag={"HB": 12000, "SL": 500},
                 merkmale=[lo.Besteuerung(date(2026, 1, 1), 1, konfession="ev", beitr_pkv=60000, beitr_pkv_basis=1),
                           lo.Besteuerung(date(2026, 7, 1), 4, faktor="0.789", kinder="1.0", konfession="rk",
                                          ehegkonfession="ev", freibetrag=lo.Freibetrag(120000, 10000, 1665, 333),
                                          hinzurechnung=lo.Freibetrag(240000, 20000, 3330, 666))])
    kor = lst26(ref_km_id=lo.km_id(neu, AG))
    for xml, sch in ((lo.lstb(neu, AG, HID, True, LIEFERANT), schema),
                     (lo.lstb(voll, AG, HID, True, LIEFERANT), schema),
                     (lo.lstb(kor, AG, HID, True, LIEFERANT, korrektur=True), schema),
                     (lo.lstb_storno(kor, AG, HID, True, LIEFERANT), storno)):
        nutz = re.search(r"<Nutzdaten>\s*(.*?)\s*</Nutzdaten>", xml, re.S).group(1)
        assert sch.validate(lxml.fromstring(nutz.encode("utf-8"))), sch.error_log
