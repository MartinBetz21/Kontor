"""ELStAM (ElsterLohn2) und Postfach (ElsterDatenabholung). Alle Daten erfunden; Steuernummer und Test-IdNr. aus den
ERiC-Beispielen (Testfinanzamt 9198, Test-IdNr. mit führender 0). ERiC läuft nur mit `KONTOR_ERIC` (lokal, VALIDIERE,
nichts geht an ELSTER); die Schemaprüfung braucht zusätzlich das ERiC-Dokumentationspaket neben dem Plattformordner."""
import re
from datetime import date
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

from kontor.core.errors import KontorError
from kontor.elster import elstam as el
from kontor.elster import eric
from kontor.elster import lohn as lo

AG = lo.Arbeitgeber("Muster GmbH", "Musterweg", "12a", "80333", "München", "9198011310134")
HID = "00000"
LIEFERANT = "Muster GmbH; Musterweg 12a; 80333 München"
IDNR = "01127645382"


def ns(xml):
    return ET.fromstring(xml.encode("utf-8"))


def attrs(root, name):
    return [dict(e.attrib) for e in root.iter() if e.tag.endswith("}" + name)]


def an(**kw):
    kw.setdefault("beginn", date(2026, 3, 1))
    kw.setdefault("referenzdatum", date(2026, 3, 1))
    return el.ElstamAN(IDNR, date(1990, 5, 17), **kw)


class TestAnfragen:
    def test_datenarten(self):
        assert el.elstam_datenart("anmelden") == "DUeAnmelden_3"
        assert el.elstam_datenart("abmelden") == "DUeAbmelden_3"
        assert el.elstam_datenart("ummelden") == el.elstam_datenart("wechsel") == "DUeUmmelden_3"
        assert el.elstam_datenart("anfrage") == "PostfachAnfrage_32"
        assert el.elstam_datenart("bestaetigung") == "PostfachBestaetigung_32"
        with pytest.raises(KontorError):
            el.elstam_datenart("quatsch")

    def test_anmelden(self):
        r = ns(el.anmelden(AG, [an(), an(hauptarbeitgeber=False, wunsch_freibetrag=120000)], HID, True, LIEFERANT))
        assert [e.text for e in r.iter() if e.tag.endswith("}Verfahren")] == ["ElsterLohn2"]
        assert [e.text for e in r.iter() if e.tag.endswith("}DatenArt")] == ["DUeAnmelden"]
        assert [e.text for e in r.iter() if e.tag.endswith("}Testmerker")] == ["700000001"]
        assert [e.text for e in r.iter() if e.tag.endswith("}Empfaenger")] == ["CS"]
        root = next(e for e in r.iter() if e.tag.endswith("}ArbeitnehmerAnmeldenRequest"))
        assert root.get("stnrAG") == "9198011310134"
        a, b = attrs(r, "ArbeitnehmerAn")
        assert a == {"idnr": IDNR, "gebdat": "19900517", "istHauptarbeitgeber": "true",
                     "beschaeftigungsbeginn": "20260301", "refDatumAG": "20260301"}
        assert b["istHauptarbeitgeber"] == "false" and b["gewuenschterFreibetrag"] == "1200"

    def test_anmelden_prueft(self):
        with pytest.raises(KontorError, match="Referenzdatum liegt vor"):
            el.anmelden(AG, [an(referenzdatum=date(2026, 2, 1))], HID, True, LIEFERANT)
        with pytest.raises(KontorError, match="Nebenarbeitgeber"):
            el.anmelden(AG, [an(wunsch_freibetrag=100)], HID, True, LIEFERANT)
        with pytest.raises(KontorError, match="IdNr"):
            el.anmelden(AG, [el.ElstamAN("123", date(1990, 1, 1), date(2026, 1, 1), date(2026, 1, 1))], HID, True, LIEFERANT)
        with pytest.raises(KontorError, match="Mindestens"):
            el.anmelden(AG, [], HID, True, LIEFERANT)
        with pytest.raises(KontorError, match="Hersteller"):
            el.anmelden(AG, [an()], "1", True, LIEFERANT)

    def test_unbekanntes_geburtsdatum(self):
        x = el.anmelden(AG, [el.ElstamAN(IDNR, "19900000", date(2026, 1, 1), date(2026, 1, 1))], HID, True, LIEFERANT)
        assert 'gebdat="19900000"' in x
        with pytest.raises(KontorError, match="Geburtsdatum"):
            el.anmelden(AG, [el.ElstamAN(IDNR, "1990-05-17", date(2026, 1, 1), date(2026, 1, 1))], HID, True, LIEFERANT)

    def test_abmelden(self):
        r = ns(el.abmelden(AG, [an(ende=date(2026, 9, 30))], HID, True, LIEFERANT))
        assert attrs(r, "ArbeitnehmerAb") == [{"idnr": IDNR, "gebdat": "19900517", "refDatumAG": "20260301",
                                               "abmeldeDatum": "20260930"}]
        with pytest.raises(KontorError, match="Beschäftigungsende"):
            el.abmelden(AG, [an()], HID, True, LIEFERANT)

    def test_datenuebermittler_wechsel(self):
        x = el.datenuebermittler_wechsel(AG, [an()], date(2026, 1, 1), HID, False, LIEFERANT)
        r = ns(x)
        assert [e.text for e in r.iter() if e.tag.endswith("}DatenArt")] == ["DUeUmmelden"]
        assert "<Testmerker>" not in x
        w = next(e for e in r.iter() if e.tag.endswith("}DatenuebermittlerWechselRequest"))
        assert w.get("stnrAG") == AG.steuernummer13 and w.get("refDatumAG") == "20260101"
        assert attrs(r, "ArbeitnehmerUm") == [{"gebdat": "19900517", "idnr": IDNR}]
        assert el.ummelden is el.datenuebermittler_wechsel


class TestPostfach:
    def test_anfrage(self):
        r = ns(el.postfach_anfrage(HID, True, LIEFERANT, ag_steuernummer="9198011310134"))
        assert [e.text for e in r.iter() if e.tag.endswith("}Verfahren")] == ["ElsterDatenabholung"]
        assert [e.text for e in r.iter() if e.tag.endswith("}DatenArt")] == ["PostfachAnfrage"]
        pa = attrs(r, "PostfachAnfrage")[0]
        assert pa == {"einschraenkung": "neue", "max": "1000"}
        assert attrs(r, "DatenartBereitstellung") == [{"name": "AenderungslisteDUe", "ag-steuernummer": "9198011310134"}]
        seite = ns(el.postfach_anfrage(HID, True, LIEFERANT, nur_neue=False, nach_bestid="abc"))
        assert attrs(seite, "PostfachAnfrage")[0]["paginierungLetzteBestId"] == "abc"

    def test_bestaetigung_teilt_in_bloecke(self):
        ids = [f"id{i:04d}" for i in range(250)] + ["id0001"]
        r = ns(el.postfach_bestaetigung(ids, HID, True, LIEFERANT))
        bloecke = [e for e in r.iter() if e.tag.endswith("}Nutzdatenblock")]
        assert [len([x for x in b.iter() if x.tag.endswith("}Bereitstellung")]) for b in bloecke] == [100, 100, 50]
        assert [e.text for e in r.iter() if e.tag.endswith("}NutzdatenTicket")] == ["1", "2", "3"]
        with pytest.raises(KontorError):
            el.postfach_bestaetigung([], HID, True, LIEFERANT)

    def test_antworten_lesen(self):
        antwort = """<Elster xmlns="http://www.elster.de/elsterxml/schema/v11"><DatenTeil><Nutzdatenblock><Nutzdaten>
        <Datenabholung xmlns="http://finkonsens.de/elster/elsterdatenabholung/v3" version="32">
        <PostfachAnfrageAntwort einschraenkung="neue" max="1000" paginierungLetzteBestId="be2">
        <DatenartBereitstellung name="AenderungslisteDUe" anzahltreffer="2"><Bereitstellungen>
        <Bereitstellung id="be1" bereitstellungsticket="t1" groesse="900"><MetaInformationen>
        <Meta name="erstelldatum">20261001</Meta></MetaInformationen>
        <Datenpaket><ReferenzId>r1</ReferenzId><Encoding>UTF-8</Encoding><Groesse>1205</Groesse></Datenpaket></Bereitstellung>
        <Bereitstellung id="be2" groesse="100"><Datenpaket><ReferenzId>r2</ReferenzId><Groesse>10</Groesse></Datenpaket>
        </Bereitstellung></Bereitstellungen></DatenartBereitstellung></PostfachAnfrageAntwort>
        </Datenabholung></Nutzdaten></Nutzdatenblock></DatenTeil></Elster>"""
        b = el.bereitstellungen_lesen(antwort)
        assert [x["id"] for x in b] == ["be1", "be2"] and b[0]["art"] == "AenderungslisteDUe"
        assert b[0]["pakete"] == [{"referenz_id": "r1", "groesse": 1205}] and b[0]["meta"] == {"erstelldatum": "20261001"}
        assert b[1]["letzte_bestid"] == "be2" and b[0]["encoding"] == "UTF-8" and b[0]["ticket"] == "t1"

    def test_xml_mit_entity_wird_abgelehnt(self):
        with pytest.raises(KontorError, match="ENTITY"):
            el.bereitstellungen_lesen('<!DOCTYPE a [<!ENTITY x "y">]><a>&x;</a>')


# -- Änderungsliste ----------------------------------------------------------------------------------------------------

NS_L = "http://finkonsens.de/lavendel/datenuebermittler/aenderungsliste/v2"
NS_C = "http://finkonsens.de/lavendel/datenuebermittler/types/complex/v2"


def liste(art, arbeitnehmer, ag_hinweis=""):
    return (f'<Aenderungsliste xmlns="{NS_L}" xmlns:ct="{NS_C}" stnrDUe="9198011310134" '
            f'transferTicket="0000000000000000001" nutzdatenTicket="1" art="{art}" laufendeNummer="202610001" '
            f'anzahlTeillisten="1" schemaversion="2"><Arbeitgeber stnrAG="9198011310134">{arbeitnehmer}'
            f'{ag_hinweis or HINWEIS_AG}</Arbeitgeber></Aenderungsliste>')


HINWEIS_AG = '<ct:AG-Verfahrenshinweis code="0" message="Verarbeitung ok" type="INFORMATION"/>'
MERKMAL_1 = ('<ct:BasisELStAM gueltigAb="20260101"><ct:Steuerklasse nummer="4"/><ct:Kinderfreibetrag anzahl="1.5"/>'
             '<ct:Kirchensteuer konfession="rk"/><ct:KirchensteuerPartner konfession="ev"/><ct:Faktor wert="912"/>'
             '<ct:Freibetrag jahr="0" monat="0" woche="0.00" tag="0.00"/></ct:BasisELStAM>')
MERKMAL_2 = ('<ct:BasisELStAM gueltigAb="20260415"><ct:Steuerklasse nummer="1"/><ct:Kinderfreibetrag anzahl="0.0"/>'
             '<ct:Kirchensteuer konfession="--"/><ct:Faktor wert="0"/>'
             '<ct:Freibetrag jahr="1200" monat="100" woche="23.33" tag="3.33"/>'
             '<ct:Hinzurechnungsbetrag jahr="2400" monat="200" woche="46.66" tag="6.66"/></ct:BasisELStAM>')
KVPV = ('<ct:ListeKVPV><ct:KVPV Besteuerungszeitraum="2026"><ct:KVPVBeitraege gueltigAb="1" gueltigBis="12">'
        '<ct:KVZuschuss monatsBeitrag="30000"/><ct:KVVorsorge monatsBeitrag="10000"/>'
        '<ct:PVZuschuss monatsBeitrag="5000"/><ct:PVVorsorge monatsBeitrag="2000"/></ct:KVPVBeitraege></ct:KVPV></ct:ListeKVPV>')
OK_HINWEIS = '<ct:AN-Verfahrenshinweis code="552000000" message="Anmeldung erfolgreich" type="INFORMATION"/>'

ANMELDEBESTAETIGUNG = liste("ANMELDEBESTAETIGUNGSLISTE", (
    f'<ct:Arbeitnehmer idnr="{IDNR}"><ct:ListeBasisELStAM>{MERKMAL_1}{MERKMAL_2}</ct:ListeBasisELStAM>{KVPV}{OK_HINWEIS}'
    '</ct:Arbeitnehmer>'
    '<ct:Arbeitnehmer idnr="02345678901"><ct:AN-Verfahrenshinweis code="552020001" message="IdNr unbekannt" '
    'type="ABLEHNUNG"/></ct:Arbeitnehmer>'))
ABMELDUNG = liste("ABMELDEBESTAETIGUNGSLISTE", (
    f'<ct:Arbeitnehmer idnr="{IDNR}"><ct:AN-Verfahrenshinweis code="552000010" message="Abmeldung verarbeitet" '
    'type="INFORMATION" datum="20260930"/></ct:Arbeitnehmer>'))
AG_ABLEHNUNG = liste("ANMELDEBESTAETIGUNGSLISTE", "", '<ct:AG-Verfahrenshinweis code="550000001" '
                     'message="Arbeitgeber nicht registriert" type="ABLEHNUNG"/>')


class TestAenderungsliste:
    def test_anmeldebestaetigung(self):
        z = [r for r in el.aenderungsliste_lesen(ANMELDEBESTAETIGUNG) if r["idnr"] == IDNR]
        assert [r["gueltig_ab"] for r in z] == [date(2026, 1, 1), date(2026, 4, 15)]
        a, b = z
        assert (a["steuerklasse"], a["faktor"], a["kinderfreibetraege"]) == (4, "0.912", "1.5")
        assert (a["konfession"], a["konfession_ehegatte"]) == ("rk", "ev")
        assert (a["freibetrag"], a["hinzurechnung"], a["freibetrag_jahr"], a["hinzurechnung_jahr"]) == (0, 0, 0, 0)
        assert (b["steuerklasse"], b["faktor"], b["kinderfreibetraege"]) == (1, "", "0.0")
        assert (b["konfession"], b["konfession_ehegatte"]) == ("", "")          # „--“ = konfessionslos
        assert (b["freibetrag"], b["freibetrag_jahr"]) == (10000, 120000)
        assert (b["hinzurechnung"], b["hinzurechnung_jahr"]) == (20000, 240000)
        assert a["art"] == "Anmeldebestätigung" and a["liste"] == "ANMELDEBESTAETIGUNGSLISTE"
        assert a["stnr_ag"] == "9198011310134" and a["code"] == 552000000 and a["text"] == "Anmeldung erfolgreich"
        assert a["roh"]["idnr"] == IDNR and a["roh"]["gueltigAb"] == "20260101"
        assert a["roh"]["Kirchensteuer"] == {"konfession": "rk"}
        kv = a["roh"]["kvpv"][0]
        assert kv["jahr"] == "2026" and kv["beitraege"][0]["KVZuschuss"] == 30000

    def test_ablehnung_mit_grund(self):
        z = [r for r in el.aenderungsliste_lesen(ANMELDEBESTAETIGUNG) if r["idnr"] == "02345678901"]
        assert len(z) == 1
        assert z[0]["art"] == "Ablehnung" and z[0]["gueltig_ab"] is None and z[0]["steuerklasse"] is None
        assert z[0]["code"] == 552020001 and z[0]["text"] == "IdNr unbekannt"

    def test_abmeldung(self):
        (z,) = el.aenderungsliste_lesen(ABMELDUNG)
        assert z["art"] == "Abmeldung" and z["gueltig_ab"] is None and z["code"] == 552000010
        assert z["hinweise"][0]["datum"] == "20260930"

    def test_ablehnung_auf_arbeitgeberebene(self):
        (z,) = el.aenderungsliste_lesen(AG_ABLEHNUNG)
        assert z["art"] == "Ablehnung" and z["idnr"] == "" and z["text"] == "Arbeitgeber nicht registriert"

    def test_liest_auch_die_ganze_antwort(self):
        wrapped = (f'<?xml version="1.0" encoding="UTF-8"?><Elster xmlns="http://www.elster.de/elsterxml/schema/v11">'
                   f'<DatenTeil><Nutzdatenblock><Nutzdaten>{ABMELDUNG}</Nutzdaten></Nutzdatenblock></DatenTeil></Elster>')
        assert el.aenderungsliste_lesen(wrapped)[0]["art"] == "Abmeldung"
        assert el.aenderungsliste_lesen("<leer/>") == []

    def test_monatsliste_ist_aenderung(self):
        x = liste("MONATSLISTE", f'<ct:Arbeitnehmer idnr="{IDNR}"><ct:ListeBasisELStAM>{MERKMAL_2}</ct:ListeBasisELStAM>'
                                 f'{OK_HINWEIS}</ct:Arbeitnehmer>')
        assert el.aenderungsliste_lesen(x)[0]["art"] == "Änderung"


# -- ERiC und Schemata (lokal) -----------------------------------------------------------------------------------------

def _eric_da():
    try:
        eric.eric_dir()
    except KontorError:
        return False
    return True


def _docs() -> Path | None:
    """Schemata aus dem lokalen ERiC-Dokumentationspaket (Lizenz: liegt nicht im Repo)."""
    if not _eric_da():
        return None
    base = eric.eric_dir().parent / "Dokumentation" / "Schnittstellenbeschreibungen" / "Sonstige"
    return base if (base / "ElsterLohn2_3" / "Schema" / "Aenderungsliste-v2.xsd").is_file() else None


def _nutz(xml: str) -> str:
    return re.search(r"<Nutzdaten>\s*(.*?)\s*</Nutzdaten>", xml, re.S).group(1)


@pytest.mark.skipif(not _eric_da(), reason="KONTOR_ERIC nicht gesetzt oder ERiC nicht vorhanden")
class TestEric:
    def _ok(self, r):
        assert r["ok"] or r["code"] == 610001003, (r["text"], r["meldungen"], r.get("log", ""))

    def test_anmelden_abmelden_wechsel(self):
        self._ok(eric.pruefen(el.anmelden(AG, [an(), an(hauptarbeitgeber=False)], HID, True, LIEFERANT),
                              el.elstam_datenart("anmelden")))
        self._ok(eric.pruefen(el.abmelden(AG, [an(ende=date(2026, 9, 30))], HID, True, LIEFERANT),
                              el.elstam_datenart("abmelden")))
        self._ok(eric.pruefen(el.datenuebermittler_wechsel(AG, [an()], date(2026, 1, 1), HID, True, LIEFERANT),
                              el.elstam_datenart("wechsel")))

    def test_fehlendes_pflichtfeld_wird_gemeldet(self):
        x = el.anmelden(AG, [an()], HID, True, LIEFERANT).replace(' idnr="01127645382"', "")
        assert not eric.pruefen(x, el.elstam_datenart("anmelden"))["ok"]

    def test_postfach(self):
        self._ok(eric.pruefen(el.postfach_anfrage(HID, True, LIEFERANT, ag_steuernummer=AG.steuernummer13),
                              el.elstam_datenart("anfrage")))
        self._ok(eric.pruefen(el.postfach_bestaetigung(["be1", "be2"], HID, True, LIEFERANT),
                              el.elstam_datenart("bestaetigung")))

    def test_otto_laedt(self):
        assert eric.otto_version().startswith("Otto")

    @pytest.mark.skipif(_docs() is None, reason="Schemata (ERiC-Dokumentationspaket) nicht vorhanden")
    def test_gegen_die_schemata(self):
        lxml = pytest.importorskip("lxml.etree")
        d = _docs() / "ElsterLohn2_3" / "Schema"
        for xml, xsd in ((el.anmelden(AG, [an(), an(hauptarbeitgeber=False, wunsch_freibetrag=120000)], HID, True, LIEFERANT),
                          "ArbeitnehmerAnmeldenRequest-v2.xsd"),
                         (el.abmelden(AG, [an(ende=date(2026, 9, 30))], HID, True, LIEFERANT),
                          "ArbeitnehmerAbmeldenRequest-v2.xsd"),
                         (el.datenuebermittler_wechsel(AG, [an()], date(2026, 1, 1), HID, True, LIEFERANT),
                          "DatenuebermittlerWechselRequest-v2.xsd")):
            s = lxml.XMLSchema(lxml.parse(str(d / xsd)))
            assert s.validate(lxml.fromstring(_nutz(xml).encode())), s.error_log

    @pytest.mark.skipif(_docs() is None, reason="Schemata (ERiC-Dokumentationspaket) nicht vorhanden")
    def test_testlisten_entsprechen_dem_schema(self):
        """Die erfundenen Änderungslisten dieses Tests sind schemagültig, der Leser liest also Echtformat."""
        lxml = pytest.importorskip("lxml.etree")
        s = lxml.XMLSchema(lxml.parse(str(_docs() / "ElsterLohn2_3" / "Schema" / "Aenderungsliste-v2.xsd")))
        for x in (ANMELDEBESTAETIGUNG, ABMELDUNG, AG_ABLEHNUNG):
            assert s.validate(lxml.fromstring(x.encode())), s.error_log

    @pytest.mark.skipif(_docs() is None, reason="Beispiele (ERiC-Dokumentationspaket) nicht vorhanden")
    def test_offizielle_beispiele_der_abholung(self):
        b = _docs() / "ElsterDatenabholung_32" / "Beispiele" / "Abholung-PostfachNachricht-Daten"
        antwort = (b / "PostfachAnfrage" / "1_b1_PostfachAnfrage_send_Auth_Response.xml").read_text(encoding="utf-8")
        liste_ = el.bereitstellungen_lesen(antwort)
        assert len(liste_) == 5 and {x["art"] for x in liste_} == {"ESB", "EPMitteilung"}
        assert liste_[0]["pakete"] == [{"referenz_id": "32550000-0000-0000-0000-000000000000", "groesse": 1205}]
        bestaetigung = (b / "PostfachBestaetigung" / "2_b2_PostfachBestaetigung_mehreren_NDB_Response.xml").read_text(
            encoding="utf-8")
        assert [x["code"] for x in el.bestaetigung_lesen(bestaetigung)][:3] == [0, 194346573, 1943226573]
        # unsere Anfrage hat dieselbe Form wie das offizielle Beispiel
        anfrage = (b / "PostfachAnfrage" / "1_a1_PostfachAnfrage_send_Auth_Request.xml").read_text(encoding="utf-8")
        ours = el.postfach_anfrage(HID, True, LIEFERANT, bereitstellung="ESB")
        assert re.findall(r"<(\w+)", _nutz(ours))[:3] == re.findall(r"<(\w+)", _nutz(anfrage))[:3]
