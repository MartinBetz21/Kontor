"""Umsatzsteuer-Voranmeldung für ELSTER (Verfahren ElsterAnmeldung, Datenart UStVA, datenartVersion `UStVA_<Jahr>`).
Rein, ohne IO: Daten rein, XML raus. Prüfen und Senden macht `eric.py`.

Schema `http://finkonsens.de/elster/elsteranmeldung/ustva/v<Jahr>` (ERiC-Schemadokumentation UStVA_2026). Die
Bemessungsgrundlagen (Kz81, 86, 21, 45, 46, 84 …) sind ganze Euro, Steuerbeträge (Kz47, 85, 66, 67, 83) Euro mit zwei
Nachkommastellen. Die Steuer zu Kz81 und Kz86 rechnet ELSTER selbst; Kz83 muss dazu passen, sonst lehnt ERiC ab."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from xml.sax.saxutils import escape

from ..core.errors import KontorError
from .lohn import Arbeitgeber, _eur, _hersteller, _hnr, _stnr, _text

TESTMERKER = "700000004"          # ERiC-Entwicklerhandbuch 6.5.5: Testfall, wird am Annahmeserver gelöscht
JAHRE = (2025, 2026)
ZEITRAEUME = {f"{m:02d}" for m in range(1, 13)} | {"41", "42", "43", "44"}

# Kennzahlen, die Kontor füllt, in der Reihenfolge des Schemas; True = ganze Euro (Bemessungsgrundlage)
KENNZAHLEN = {"21": True, "45": True, "46": True, "47": False, "66": False, "67": False, "81": True, "83": False,
              "84": True, "85": False, "86": True}
_STEUERSATZ = {"81": 19, "86": 7}


@dataclass
class UStVA:
    """`kz`: Kennzahl → Betrag. Bemessungsgrundlagen in ganzen Euro, Steuerbeträge in Cent. Kz83 rechnet `kz83`."""
    jahr: int
    zeitraum: str                     # 01–12 oder 41–44 (Quartale)
    kz: dict[str, int] = field(default_factory=dict)
    berichtigt: bool = False
    erstellt: date | None = None
    widnr: str = ""                   # Wirtschafts-IdNr. (heute die USt-IdNr., DE + 9 Ziffern); ohne sie ein Hinweis

    @property
    def kz83(self) -> int:
        """Verbleibende Vorauszahlung (negativ: Überschuss) in Cent, wie ELSTER sie aus den Feldern rechnet."""
        k = self.kz
        return (sum(k.get(n, 0) * s for n, s in _STEUERSATZ.items()) + k.get("47", 0) + k.get("85", 0)
                - k.get("66", 0) - k.get("67", 0))


def datenart(jahr: int) -> str:
    if jahr not in JAHRE:
        raise KontorError(f"Umsatzsteuer-Voranmeldung {jahr}: Schema nicht hinterlegt "
                          f"(vorhanden: {', '.join(map(str, JAHRE))}).")
    return f"UStVA_{jahr}"


def zeitraum(spec: str) -> tuple[int, str]:
    """Kontor-Zeitraum (`2026-09`, `2026-Q3`) → (Jahr, ELSTER-Zeitraum `09` bzw. `43`)."""
    jahr, _, teil = spec.partition("-")
    if not jahr.isdigit() or not teil:
        raise KontorError(f"Zeitraum {spec!r}: Voranmeldung nur für Monat (JJJJ-MM) oder Quartal (JJJJ-Qn).")
    z = f"4{teil[1:]}" if teil.startswith("Q") else teil
    if z not in ZEITRAEUME:
        raise KontorError(f"Zeitraum {spec!r}: Voranmeldung nur für Monat (JJJJ-MM) oder Quartal (JJJJ-Qn).")
    return int(jahr), z


def _kz_zeilen(a: UStVA) -> list[str]:
    unbekannt = set(a.kz) - set(KENNZAHLEN) - {"83"}
    if unbekannt:
        raise KontorError(f"Kennzahl {', '.join(sorted(unbekannt))}: nicht abgebildet.")
    k = []
    for n, ganz in KENNZAHLEN.items():
        wert = a.kz83 if n == "83" else a.kz.get(n, 0)
        if n != "83" and not wert:
            continue
        if ganz and wert < 0 and n not in ("81", "86"):
            raise KontorError(f"Kz{n}: negativer Betrag.")
        k.append(f"<Kz{n}>{wert if ganz else _eur(wert)}</Kz{n}>")
    return k


def ustva(a: UStVA, u: Arbeitgeber, hersteller_id: str, test: bool, datenlieferant: str, version: str = "0.1") -> str:
    """Der vollständige ELSTER-Datensatz (Verfahren ElsterAnmeldung, Datenart UStVA, Vorgang send-Auth). `u` ist der
    Unternehmer (Name und Anschrift wie beim Finanzamt, 13-stellige Steuernummer)."""
    datenart(a.jahr)
    if a.zeitraum not in ZEITRAEUME:
        raise KontorError(f"Zeitraum „{a.zeitraum}“: erlaubt sind 01–12 und 41–44.")
    st, hid = _stnr(u), _hersteller(hersteller_id)
    if a.widnr and not re.fullmatch(r"DE\d{9}", a.widnr):
        raise KontorError("Wirtschafts-IdNr.: DE und neun Ziffern erwartet.")
    hnr, zusatz = _hnr(u.hausnummer)
    erstellt = (a.erstellt or date.today()).strftime("%Y%m%d")
    testmerker = f"<Testmerker>{TESTMERKER}</Testmerker>" if test else ""
    lieferant = escape(datenlieferant)
    plz = _text("PLZ", u.plz, 5)
    kontakt = (f"<Telefon>{_text('Telefon', u.telefon, 20)}</Telefon>" if u.telefon else "") \
        + (f"<Email>{_text('E-Mail', u.email, 70)}</Email>" if u.email else "")
    unternehmer = (f"<Bezeichnung>{_text('Name', u.name, 50)}</Bezeichnung>"
                   f"<Str>{_text('Straße', u.strasse, 33)}</Str><Hausnummer>{hnr}</Hausnummer>"
                   + (f"<HNrZusatz>{_text('Hausnummernzusatz', zusatz, 15)}</HNrZusatz>" if zusatz else "")
                   + f"<Ort>{_text('Ort', u.ort, 34)}</Ort><PLZ>{plz}</PLZ>{kontakt}")
    strasse_komplett = f"{u.strasse.strip()} {u.hausnummer.strip()}"
    kennzahlen = "\n".join(_kz_zeilen(a))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Elster xmlns="http://www.elster.de/elsterxml/schema/v11">
<TransferHeader version="11">
<Verfahren>ElsterAnmeldung</Verfahren><DatenArt>UStVA</DatenArt><Vorgang>send-Auth</Vorgang>{testmerker}
<HerstellerID>{hid}</HerstellerID><DatenLieferant>{lieferant}</DatenLieferant>
<Datei><Verschluesselung>CMSEncryptedData</Verschluesselung><Kompression>GZIP</Kompression><TransportSchluessel/></Datei>
<VersionClient>Kontor {escape(version)}</VersionClient>
</TransferHeader>
<DatenTeil><Nutzdatenblock>
<NutzdatenHeader version="11"><NutzdatenTicket>{a.jahr}{a.zeitraum}01</NutzdatenTicket><Empfaenger id="F">{st[:4]}</Empfaenger>
<Hersteller><ProduktName>Kontor</ProduktName><ProduktVersion>{escape(version)}</ProduktVersion></Hersteller>
<DatenLieferant>{lieferant}</DatenLieferant></NutzdatenHeader>
<Nutzdaten>
<Anmeldungssteuern xmlns="http://finkonsens.de/elster/elsteranmeldung/ustva/v{a.jahr}" version="{a.jahr}">
<Erstellungsdatum>{erstellt}</Erstellungsdatum>
<DatenLieferant><Name>{_text('Name', u.name, 45)}</Name><Strasse>{_text('Straße mit Nummer', strasse_komplett, 30)}</Strasse>\
<PLZ>{plz}</PLZ><Ort>{_text('Ort', u.ort, 30)}</Ort>{kontakt}</DatenLieferant>
<Steuerfall>
<Unternehmer>{unternehmer}</Unternehmer>
<Umsatzsteuervoranmeldung>
<Jahr>{a.jahr}</Jahr><Zeitraum>{a.zeitraum}</Zeitraum><Steuernummer>{st}</Steuernummer>{f"<WIdNr>{a.widnr}</WIdNr>" if a.widnr else ""}<Kz09>{hid}</Kz09>
{"<Kz10>1</Kz10>" if a.berichtigt else ""}
{kennzahlen}
</Umsatzsteuervoranmeldung>
</Steuerfall>
</Anmeldungssteuern>
</Nutzdaten>
</Nutzdatenblock></DatenTeil>
</Elster>
"""
