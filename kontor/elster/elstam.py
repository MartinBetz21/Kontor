"""ELStAM (Verfahren ElsterLohn2, Datenübermittler): Anmelden, Abmelden, Datenübermittlerwechsel, und das Abholen der
Antworten aus dem ELSTER-Postfach (Verfahren ElsterDatenabholung). Rein, ohne IO: Daten rein, XML raus, und ein Leser
für die Änderungslisten. Prüfen, Senden und Abholen macht `eric.py`.

Ablauf (Quelle: ERiC-Entwicklerhandbuch 9.2 und 9.6, Schnittstellenbeschreibungen ElsterLohn2_3 und ElsterDatenabholung_32):
1. Der Datenübermittler sendet eine Anmeldung, Abmeldung oder einen Wechsel (datenartVersion `DUeAnmelden_3` ...).
   Die Antwort des Servers bestätigt nur den Eingang; ERiC prüft Nutzdaten nur gegen die Plausiregeln, nicht gegen das Schema.
2. Die Antworten mit den ELStAM liegen später als Änderungsliste (Bereitstellungsdatenart `AenderungslisteDUe`) im Postfach:
   `PostfachAnfrage_32` listet sie, jede Liste wird vom OTTER-Server geholt (ReferenzId), mit `EricDekodiereDaten`
   entschlüsselt und mit `PostfachBestaetigung_32` bestätigt, spätestens nach 24 Stunden.
3. `aenderungsliste_lesen` macht aus der entschlüsselten Liste einen Satz je Arbeitnehmer und Gültigkeitsdatum."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from xml.sax.saxutils import escape, quoteattr

from ..core.errors import KontorError
from .lohn import Arbeitgeber, _hersteller, _stnr

TESTMERKER = "700000001"            # Testfall: wird am Annahmeserver sofort gelöscht (ElsterLohn2: 700000001 oder 700000004)
ABHOLEN_VERSION = "32"
_ARTEN = {"anmelden": "DUeAnmelden_3", "abmelden": "DUeAbmelden_3", "ummelden": "DUeUmmelden_3",
          "wechsel": "DUeUmmelden_3"}
_ABHOLUNG = {"anfrage": f"PostfachAnfrage_{ABHOLEN_VERSION}", "bestaetigung": f"PostfachBestaetigung_{ABHOLEN_VERSION}"}


@dataclass
class ElstamAN:
    """Ein Arbeitnehmer in einer ELStAM-Anfrage. Beträge in Cent."""
    idnr: str                          # Steuer-IdNr., 11 Ziffern
    geburtsdatum: "date | str"         # zur Prüfung; unbekannte Teile als Nullen: "19500000", "00000000"
    beginn: date | None = None         # Beschäftigungsbeginn laut Vertrag (Anmeldung)
    referenzdatum: date | None = None  # Anmeldung: ab wann ELStAM kommen (frühestens Beginn); Abmeldung: das der Anmeldung
    hauptarbeitgeber: bool = True      # Anmeldung: Haupt- (True) oder Nebenarbeitgeber
    ende: date | None = None           # Abmeldung: Ende des Beschäftigungsverhältnisses
    wunsch_freibetrag: int | None = None   # Nebenarbeitgeber: gewünschter Jahresfreibetrag aus Hinzurechnung, ganze Euro


def elstam_datenart(art: str) -> str:
    """datenartVersion für ERiC: `anmelden`, `abmelden`, `ummelden`/`wechsel` (ElsterLohn2), `anfrage`/`bestaetigung`
    (ElsterDatenabholung)."""
    try:
        return {**_ARTEN, **_ABHOLUNG}[art]
    except KeyError:
        raise KontorError(f"ELStAM-Art „{art}“ unbekannt (anmelden, abmelden, ummelden, wechsel, anfrage, bestaetigung).")


def _datum(d: "date | str", feld: str, geburt: bool = False) -> str:
    if isinstance(d, date):
        return f"{d:%Y%m%d}"
    if geburt and re.fullmatch(r"(18[89]\d|(19|20)\d\d|0000)(0\d|1[0-2])[0-3]\d", d or ""):
        return d
    raise KontorError(f"{feld}: Datum erwartet (JJJJMMTT).")


def _idnr(idnr: str) -> str:
    if not re.fullmatch(r"\d{11}", idnr or ""):
        raise KontorError("IdNr: 11 Ziffern erwartet.")
    return idnr


def _rahmen(art: str, ag: Arbeitgeber, hersteller_id: str, test: bool, datenlieferant: str, version: str,
            nutzdaten: str) -> str:
    _stnr(ag)
    hid = _hersteller(hersteller_id)
    testmerker = f"<Testmerker>{TESTMERKER}</Testmerker>" if test else ""
    lieferant = escape(datenlieferant)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Elster xmlns="http://www.elster.de/elsterxml/schema/v11">
<TransferHeader version="11">
<Verfahren>ElsterLohn2</Verfahren><DatenArt>{_ARTEN[art][:-2]}</DatenArt><Vorgang>send-Auth</Vorgang>{testmerker}
<HerstellerID>{hid}</HerstellerID><DatenLieferant>{lieferant}</DatenLieferant>
<Datei><Verschluesselung>CMSEncryptedData</Verschluesselung><Kompression>GZIP</Kompression><TransportSchluessel/></Datei>
<VersionClient>Kontor {escape(version)}</VersionClient>
</TransferHeader>
<DatenTeil><Nutzdatenblock>
<NutzdatenHeader version="11"><NutzdatenTicket>1</NutzdatenTicket><Empfaenger id="L">CS</Empfaenger>
<Hersteller><ProduktName>Kontor</ProduktName><ProduktVersion>{escape(version)}</ProduktVersion></Hersteller>
<DatenLieferant>{lieferant}</DatenLieferant></NutzdatenHeader>
<Nutzdaten>
{nutzdaten}
</Nutzdaten>
</Nutzdatenblock></DatenTeil>
</Elster>
"""


def _liste(arbeitnehmer: list[ElstamAN]) -> list[ElstamAN]:
    if not arbeitnehmer:
        raise KontorError("Mindestens ein Arbeitnehmer nötig.")
    if len(arbeitnehmer) > 50000:
        raise KontorError("Höchstens 50.000 Arbeitnehmer je Anfrage.")
    return arbeitnehmer


def anmelden(ag: Arbeitgeber, arbeitnehmer: list[ElstamAN], hersteller_id: str, test: bool, datenlieferant: str,
             version: str = "0.1") -> str:
    """Anmeldung (ArbeitnehmerAnmeldenRequest): ab `referenzdatum` liefert ELSTER die ELStAM an den Datenübermittler.
    Auch der Weg, den Freibetrag eines Nebenarbeitgebers zu ändern (Referenzdatum = Datum, ab dem er gelten soll)."""
    st = _stnr(ag)
    zeilen = []
    for a in _liste(arbeitnehmer):
        if a.beginn is None or a.referenzdatum is None:
            raise KontorError("Anmeldung: Beschäftigungsbeginn und Referenzdatum nötig.")
        if a.referenzdatum < a.beginn:
            raise KontorError("Referenzdatum liegt vor dem Beschäftigungsbeginn.")
        wunsch = ""
        if a.wunsch_freibetrag is not None:
            if a.hauptarbeitgeber:
                raise KontorError("Gewünschter Freibetrag gibt es nur für Nebenarbeitgeber.")
            if a.wunsch_freibetrag % 100 or not 0 <= a.wunsch_freibetrag <= 99999999900:
                raise KontorError("Gewünschter Freibetrag: ganze Euro, höchstens 999.999.999.")
            wunsch = f' gewuenschterFreibetrag="{a.wunsch_freibetrag // 100}"'
        zeilen.append(f'<ArbeitnehmerAn idnr="{_idnr(a.idnr)}" gebdat="{_datum(a.geburtsdatum, "Geburtsdatum", True)}" '
                      f'istHauptarbeitgeber="{"true" if a.hauptarbeitgeber else "false"}"{wunsch} '
                      f'beschaeftigungsbeginn="{_datum(a.beginn, "Beschäftigungsbeginn")}" '
                      f'refDatumAG="{_datum(a.referenzdatum, "Referenzdatum")}"/>')
    nutz = (f'<ArbeitnehmerAnmeldenRequest xmlns="http://finkonsens.de/lavendel/datenuebermittler/'
            f'arbeitnehmeranmeldenrequest/v2" stnrAG="{st}">{"".join(zeilen)}</ArbeitnehmerAnmeldenRequest>')
    return _rahmen("anmelden", ag, hersteller_id, test, datenlieferant, version, nutz)


def abmelden(ag: Arbeitgeber, arbeitnehmer: list[ElstamAN], hersteller_id: str, test: bool, datenlieferant: str,
             version: str = "0.1") -> str:
    """Abmeldung (ArbeitnehmerAbmeldenRequest): `referenzdatum` ist das der Anmeldung, `ende` das Beschäftigungsende."""
    st = _stnr(ag)
    zeilen = []
    for a in _liste(arbeitnehmer):
        if a.ende is None or a.referenzdatum is None:
            raise KontorError("Abmeldung: Beschäftigungsende und das Referenzdatum der Anmeldung nötig.")
        zeilen.append(f'<ArbeitnehmerAb idnr="{_idnr(a.idnr)}" gebdat="{_datum(a.geburtsdatum, "Geburtsdatum", True)}" '
                      f'refDatumAG="{_datum(a.referenzdatum, "Referenzdatum")}" '
                      f'abmeldeDatum="{_datum(a.ende, "Beschäftigungsende")}"/>')
    nutz = (f'<ArbeitnehmerAbmeldenRequest xmlns="http://finkonsens.de/lavendel/datenuebermittler/'
            f'arbeitnehmerabmeldenrequest/v2" stnrAG="{st}">{"".join(zeilen)}</ArbeitnehmerAbmeldenRequest>')
    return _rahmen("abmelden", ag, hersteller_id, test, datenlieferant, version, nutz)


def datenuebermittler_wechsel(ag: Arbeitgeber, arbeitnehmer: list[ElstamAN], referenzdatum: date, hersteller_id: str,
                              test: bool, datenlieferant: str, version: str = "0.1") -> str:
    """Wechsel des Datenübermittlers (DatenuebermittlerWechselRequest, datenartVersion `DUeUmmelden_3`): Der Absender
    (das Zertifikat) wird für die genannten, beim Arbeitgeber schon angemeldeten Arbeitnehmer der neue Datenübermittler.
    `referenzdatum`: ab wann die ELStAM an ihn gehen (Einschränkungen in der ELStAM-Schnittstellenbeschreibung ZK).
    Je Arbeitnehmer genügen IdNr und Geburtsdatum."""
    st = _stnr(ag)
    zeilen = [f'<ArbeitnehmerUm gebdat="{_datum(a.geburtsdatum, "Geburtsdatum", True)}" idnr="{_idnr(a.idnr)}"/>'
              for a in _liste(arbeitnehmer)]
    nutz = (f'<DatenuebermittlerWechselRequest xmlns="http://finkonsens.de/lavendel/datenuebermittler/'
            f'datenuebermittlerwechselrequest/v2" refDatumAG="{_datum(referenzdatum, "Referenzdatum")}" stnrAG="{st}">'
            f'{"".join(zeilen)}</DatenuebermittlerWechselRequest>')
    return _rahmen("wechsel", ag, hersteller_id, test, datenlieferant, version, nutz)


ummelden = datenuebermittler_wechsel


# -- Postfach (ElsterDatenabholung 32) -------------------------------------------------------------------------------

def _abholung_rahmen(art: str, hersteller_id: str, test: bool, datenlieferant: str, version: str,
                     bloecke: list[str]) -> str:
    hid = _hersteller(hersteller_id)
    testmerker = f"<Testmerker>{TESTMERKER}</Testmerker>" if test else ""
    lieferant = escape(datenlieferant)
    teile = "\n".join(
        f'<Nutzdatenblock><NutzdatenHeader version="11"><NutzdatenTicket>{i}</NutzdatenTicket>'
        f'<Empfaenger id="L">CS</Empfaenger><Hersteller><ProduktName>Kontor</ProduktName>'
        f'<ProduktVersion>{escape(version)}</ProduktVersion></Hersteller><DatenLieferant>{lieferant}</DatenLieferant>'
        f'</NutzdatenHeader><Nutzdaten><Datenabholung xmlns="http://finkonsens.de/elster/elsterdatenabholung/v3" '
        f'version="{ABHOLEN_VERSION}">{b}</Datenabholung></Nutzdaten></Nutzdatenblock>'
        for i, b in enumerate(bloecke, 1))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Elster xmlns="http://www.elster.de/elsterxml/schema/v11">
<TransferHeader version="11">
<Verfahren>ElsterDatenabholung</Verfahren><DatenArt>{art}</DatenArt><Vorgang>send-Auth</Vorgang>{testmerker}
<HerstellerID>{hid}</HerstellerID><DatenLieferant>{lieferant}</DatenLieferant>
<Datei><Verschluesselung>CMSEncryptedData</Verschluesselung><Kompression>GZIP</Kompression><TransportSchluessel/></Datei>
<VersionClient>Kontor {escape(version)}</VersionClient>
</TransferHeader>
<DatenTeil>
{teile}
</DatenTeil>
</Elster>
"""


def postfach_anfrage(hersteller_id: str, test: bool, datenlieferant: str, ag_steuernummer: str = "",
                     nur_neue: bool = True, max_treffer: int = 1000, bereitstellung: str = "AenderungslisteDUe",
                     nach_bestid: str = "", version: str = "0.1") -> str:
    """PostfachAnfrage: welche Änderungslisten liegen bereit? Die Antwort nennt je Liste die Bereitstellungs-ID (für die
    Bestätigung) und die ReferenzId (zum Abholen). Mit `nach_bestid` (Wert `letzte_bestid` der Antwort) geht es bei mehr
    als `max_treffer` Treffern auf der nächsten Seite weiter."""
    if not 1 <= max_treffer <= 10000:
        raise KontorError("max_treffer: 1 bis 10000.")
    stnr = f' ag-steuernummer={quoteattr(ag_steuernummer)}' if ag_steuernummer else ""
    seite = f' paginierungLetzteBestId={quoteattr(nach_bestid)}' if nach_bestid else ""
    block = (f'<PostfachAnfrage einschraenkung="{"neue" if nur_neue else "alle"}" max="{max_treffer}"{seite}>'
             f'<DatenartBereitstellung name={quoteattr(bereitstellung)}{stnr}/></PostfachAnfrage>')
    return _abholung_rahmen("PostfachAnfrage", hersteller_id, test, datenlieferant, version, [block])


def postfach_bestaetigung(ids: list[str], hersteller_id: str, test: bool, datenlieferant: str,
                          version: str = "0.1") -> str:
    """PostfachBestaetigung: die Bereitstellungs-IDs der erfolgreich abgeholten Listen (bis zu 100 je Block; mehr
    verteilt die Funktion auf mehrere Nutzdatenblöcke). Muss spätestens 24 Stunden nach der Abholung raus."""
    ids = list(dict.fromkeys(i for i in ids if i))
    if not ids:
        raise KontorError("Keine Bereitstellungs-ID zum Bestätigen.")
    if len(ids) > 10000:
        raise KontorError("Höchstens 10.000 IDs je Bestätigung.")
    blocks = [("<PostfachBestaetigung><Bereitstellungen>"
               + "".join(f"<Bereitstellung id={quoteattr(i)}/>" for i in ids[k:k + 100])
               + "</Bereitstellungen></PostfachBestaetigung>") for k in range(0, len(ids), 100)]
    return _abholung_rahmen("PostfachBestaetigung", hersteller_id, test, datenlieferant, version, blocks)


# -- Lesen -----------------------------------------------------------------------------------------------------------

def _parse(xml: str) -> ET.Element:
    if re.search(r"<!(DOCTYPE|ENTITY)", xml):
        raise KontorError("XML mit DOCTYPE oder ENTITY wird nicht gelesen.")
    try:
        return ET.fromstring(xml.strip().encode("utf-8") if xml.lstrip().startswith("<?xml") else xml.strip())
    except ET.ParseError as e:
        raise KontorError(f"XML nicht lesbar: {e}")


def _n(el: ET.Element) -> str:
    return el.tag.rsplit("}", 1)[-1]


def _kinder(el: ET.Element, name: str) -> list[ET.Element]:
    return [c for c in el if _n(c) == name]


def _alle(root: ET.Element, name: str) -> list[ET.Element]:
    return [e for e in root.iter() if _n(e) == name]


def bereitstellungen_lesen(xml: str) -> list[dict]:
    """Antwort auf eine PostfachAnfrage → eine Zeile je Bereitstellung: `art`, `id` (zum Bestätigen), `ticket`,
    `groesse`, `meta` (dict), `pakete` (je Datenpaket und Anhang `referenz_id` zum Abholen und `groesse`), `referenz_ids`,
    `encoding`, `letzte_bestid`."""
    root = _parse(xml)
    out = []
    for antwort in _alle(root, "PostfachAnfrageAntwort"):
        letzte = antwort.get("paginierungLetzteBestId", "")
        for art in _kinder(antwort, "DatenartBereitstellung"):
            for bs in _alle(art, "Bereitstellung"):
                meta = {m.get("name", ""): (m.text or "").strip() for m in _alle(bs, "Meta")}
                pakete = []
                for dp in _alle(bs, "Datenpaket"):
                    rid = next(iter(_alle(dp, "ReferenzId")), None)
                    gr = next(iter(_alle(dp, "Groesse")), None)
                    if rid is not None:
                        pakete.append({"referenz_id": (rid.text or "").strip(), "groesse": int((gr.text or 0) if gr is not None else 0)})
                for an in _alle(bs, "Anhang"):
                    rid = next(iter(_alle(an, "DateiReferenzId")), None)
                    gr = next(iter(_alle(an, "DateiGroesse")), None)
                    if rid is not None:
                        pakete.append({"referenz_id": (rid.text or "").strip(), "groesse": int((gr.text or 0) if gr is not None else 0)})
                enc = next((((e.text or "").strip()) for e in _alle(bs, "Encoding")), "")
                out.append({"art": art.get("name", ""), "id": bs.get("id", ""),
                            "ticket": bs.get("bereitstellungsticket", ""), "groesse": int(bs.get("groesse") or 0),
                            "meta": meta, "pakete": pakete, "referenz_ids": [x["referenz_id"] for x in pakete],
                            "encoding": enc, "letzte_bestid": letzte})
    return out


def bestaetigung_lesen(xml: str) -> list[dict]:
    """Antwort auf eine PostfachBestaetigung → `id`, `code` (0 = bestätigt), `text` je Bereitstellung."""
    out = []
    for bs in _alle(_parse(xml), "Bereitstellung"):
        r = next(iter(_alle(bs, "Rueckgabe")), None)
        if r is not None:
            out.append({"id": bs.get("id", ""), "code": int(next(iter(_alle(r, "Code"))).text or 0),
                        "text": (next(iter(_alle(r, "Text"))).text or "").strip()})
    return out


_LISTEN = {"ANMELDEBESTAETIGUNGSLISTE": "Anmeldebestätigung", "ABMELDEBESTAETIGUNGSLISTE": "Abmeldung",
           "UMMELDEBESTAETIGUNGSLISTE": "Ummeldung", "MONATSLISTE": "Änderung", "BRUTTOLISTE": "Bruttoliste"}


def _datum_lesen(s: str | None) -> date | None:
    return date(int(s[:4]), int(s[4:6]), int(s[6:8])) if s and re.fullmatch(r"\d{8}", s) else None


def _cents(s: str | None) -> int:
    return int((Decimal(s or "0") * 100).to_integral_value())


def _merkmal(b: ET.Element) -> dict:
    def attr(name: str, key: str) -> str | None:
        el = next(iter(_kinder(b, name)), None)
        return None if el is None else el.get(key)

    faktor = int(attr("Faktor", "wert") or 0)
    kinder = attr("Kinderfreibetrag", "anzahl")
    konf, partner = attr("Kirchensteuer", "konfession") or "", attr("KirchensteuerPartner", "konfession") or ""
    fb = next(iter(_kinder(b, "Freibetrag")), None)
    hz = next(iter(_kinder(b, "Hinzurechnungsbetrag")), None)
    return {"gueltig_ab": _datum_lesen(b.get("gueltigAb")), "steuerklasse": int(attr("Steuerklasse", "nummer") or 0),
            "faktor": f"0.{faktor:03d}" if faktor else "",
            "kinderfreibetraege": str(Decimal(kinder).quantize(Decimal("0.1"))) if kinder else "0.0",
            "konfession": "" if konf == "--" else konf, "konfession_ehegatte": "" if partner == "--" else partner,
            "freibetrag": _cents(fb.get("monat")) if fb is not None else 0,
            "hinzurechnung": _cents(hz.get("monat")) if hz is not None else 0,
            "freibetrag_jahr": _cents(fb.get("jahr")) if fb is not None else 0,
            "hinzurechnung_jahr": _cents(hz.get("jahr")) if hz is not None else 0}


def aenderungsliste_lesen(xml: str) -> list[dict]:
    """Entschlüsselte Änderungsliste (auch die ganze ELSTER-Antwort) → eine Zeile je Arbeitnehmer und Gültigkeitsdatum.

    Schlüssel: `idnr`, `gueltig_ab` (date), `steuerklasse` (int), `faktor` ("0.912" oder ""), `kinderfreibetraege` ("1.5"),
    `konfession`, `konfession_ehegatte` ("ev", "rk" ... oder ""), `freibetrag`, `hinzurechnung` (Monatsbetrag, Cent),
    `freibetrag_jahr`, `hinzurechnung_jahr` (Cent); dazu `art` ("Anmeldebestätigung", "Abmeldung", "Ummeldung", "Änderung",
    "Bruttoliste" oder "Ablehnung"), `liste` (Art der Liste im Original), `stnr_ag`, `code`, `text`, `hinweise` (alle
    Verfahrenshinweise des Arbeitnehmers) und `roh` (die Attribute des Arbeitnehmers und der Merkmale, unverändert).
    Arbeitnehmer ohne Besteuerungsmerkmale (Abmeldung, Ablehnung, Fehler) kommen mit `gueltig_ab = None` und Text. Eine
    Ablehnung auf Arbeitgeberebene ohne Arbeitnehmer kommt mit leerer `idnr`."""
    out: list[dict] = []
    for liste in _alle(_parse(xml), "Aenderungsliste"):
        art_liste = liste.get("art", "")
        for ag in _kinder(liste, "Arbeitgeber"):
            stnr = ag.get("stnrAG", "")
            ag_hinweise = [dict(h.attrib) for h in _kinder(ag, "AG-Verfahrenshinweis")]
            for an in _kinder(ag, "Arbeitnehmer"):
                hinweise = [dict(h.attrib) for h in _kinder(an, "AN-Verfahrenshinweis")]
                abgelehnt = [h for h in hinweise if h.get("type") in ("ABLEHNUNG", "FEHLER")]
                zeile_hinweis = (abgelehnt or hinweise or [{}])[0]
                basis = {"idnr": an.get("idnr", ""), "liste": art_liste, "stnr_ag": stnr,
                         "code": int(zeile_hinweis["code"]) if zeile_hinweis.get("code") else None,
                         "text": zeile_hinweis.get("message", ""), "hinweise": hinweise}
                merkmale = [m for lb in _kinder(an, "ListeBasisELStAM") for m in _kinder(lb, "BasisELStAM")]
                roh_an = dict(an.attrib)
                kvpv = [{"jahr": k.get("Besteuerungszeitraum"), "beitraege": [
                    {"von": i.get("gueltigAb"), "bis": i.get("gueltigBis"),
                     **{_n(x): int(x.get("monatsBeitrag") or 0) for x in i}} for i in _alle(k, "KVPVBeitraege")]}
                    for lk in _kinder(an, "ListeKVPV") for k in _kinder(lk, "KVPV")]
                art = "Ablehnung" if abgelehnt else _LISTEN.get(art_liste, art_liste.capitalize())
                if not merkmale:
                    out.append({**basis, "gueltig_ab": None, "steuerklasse": None, "faktor": "",
                                "kinderfreibetraege": "", "konfession": "", "konfession_ehegatte": "", "freibetrag": 0,
                                "hinzurechnung": 0, "freibetrag_jahr": 0, "hinzurechnung_jahr": 0, "art": art,
                                "roh": {**roh_an, "kvpv": kvpv}})
                for m in merkmale:
                    z = _merkmal(m)
                    out.append({**basis, **z, "art": art,
                                "roh": {**roh_an, **dict(m.attrib), "kvpv": kvpv,
                                        **{_n(c): dict(c.attrib) for c in m}}})
            ag_abgelehnt = [h for h in ag_hinweise if h.get("type") in ("ABLEHNUNG", "FEHLER")]
            if ag_abgelehnt and not _kinder(ag, "Arbeitnehmer"):
                h = ag_abgelehnt[0]
                out.append({"idnr": "", "liste": art_liste, "stnr_ag": stnr, "code": int(h.get("code") or 0),
                            "text": h.get("message", ""), "hinweise": ag_hinweise, "gueltig_ab": None,
                            "steuerklasse": None, "faktor": "", "kinderfreibetraege": "", "konfession": "",
                            "konfession_ehegatte": "", "freibetrag": 0, "hinzurechnung": 0, "freibetrag_jahr": 0,
                            "hinzurechnung_jahr": 0, "art": "Ablehnung", "roh": {"ag": ag_hinweise}})
    return out
