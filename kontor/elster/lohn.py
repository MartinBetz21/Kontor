"""Lohn-Datensätze für ELSTER (Spec Lohn): Lohnsteuer-Anmeldung (LStA) und elektronische Lohnsteuerbescheinigung (LStB).
Rein, ohne IO: Daten rein, XML raus. Prüfen und Senden macht `eric.py`.

LStA: Verfahren ElsterAnmeldung, datenartVersion `LStA_<Jahr>` (Schema 2025 und 2026 liegen vor), ERiC prüft Schema und
Plausibilitätsregeln. Beträge in Euro mit zwei Nachkommastellen, Punkt als Trenner.

LStB: Verfahren ElsterLohn, datenartVersion `ElsterLohn`, Schema lstb202501 (nur Lohnjahr 2025). ERiC prüft hier die
Nutzdaten NICHT und druckt nichts; Fehler kommen erst im Verarbeitungsprotokoll (Datenabholung).
Ab Lohnjahr 2026 gilt die Datenart LSTMitteilung im Verfahren ElsterKMV (datenartVersion `ElsterKMV`, KONSENS-Mitteilung
Version 7 mit LST-Detailversion 2, Schema und Beschreibung öffentlich auf esteuer.de). `lstb` und `lstb_storno` wählen
nach dem Lohnjahr den Weg; die Felder sind dieselben, bis auf Nr. 28 (entfällt, siehe `Besteuerung.beitr_pkv`).
Die IdNr genügt, die eTIN gibt es nicht mehr."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from xml.sax.saxutils import escape, quoteattr

from ..core.errors import KontorError

TESTMERKER_LSTA = "700000004"     # ERiC-Entwicklerhandbuch 6.5.5: Testfall, wird am Annahmeserver gelöscht
TESTMERKER_LSTB = "220000000"     # Testfall LStB: nur Validierung am Annahmeserver und Protokoll
LSTA_JAHRE = (2025, 2026)
LSTB_JAHRE = (2025,)
LSTMITTEILUNG_JAHRE = (2026,)
TESTMERKER_KMV = "240000000"      # Testfall ElsterKMV (ERiC-Entwicklerhandbuch Tabelle 6-7)

# Bundesland des Finanzamts aus den ersten zwei Stellen der Bundesfinanzamtsnummer (Empfänger der LStB)
_LAND = {"10": "SL", "11": "BE", "21": "SH", "22": "HH", "23": "NI", "24": "HB", "26": "HE", "27": "RP", "28": "BW",
         "30": "BB", "31": "ST", "32": "SN", "40": "MV", "41": "TH", "51": "NW", "52": "NW", "53": "NW", "54": "NW",
         "55": "NW", "91": "BY", "92": "BY"}


# -- gemeinsam -----------------------------------------------------------------------------------------------------

@dataclass
class Arbeitgeber:
    """Betriebsstätte. `finanzamt` (Name des Finanzamts) braucht nur die LStB."""
    name: str
    strasse: str
    hausnummer: str
    plz: str
    ort: str
    steuernummer13: str                # ELSTER-Format der Betriebsstätten-Steuernummer, 13 Stellen
    telefon: str = ""
    email: str = ""
    finanzamt: str = ""


def _eur(cents: int) -> str:
    if abs(cents) >= 10 ** 13:
        raise KontorError(f"Betrag {cents / 100:.2f} ist zu groß für den ELSTER-Datensatz.")
    return f"{'-' if cents < 0 else ''}{abs(cents) // 100}.{abs(cents) % 100:02d}"


def _text(feld: str, wert: str, maxlen: int, pflicht: bool = True) -> str:
    wert = (wert or "").strip()
    if not wert and pflicht:
        raise KontorError(f"{feld}: Angabe fehlt.")
    if len(wert) > maxlen:
        raise KontorError(f"{feld}: {len(wert)} Zeichen, ELSTER erlaubt höchstens {maxlen}.")
    return escape(wert)


def _stnr(ag: Arbeitgeber) -> str:
    if not re.fullmatch(r"\d{4}0\d{8}", ag.steuernummer13 or ""):
        raise KontorError("Steuernummer: 13-stelliges ELSTER-Format erwartet (vierstelliges Finanzamt, dann 0 und 8 Ziffern).")
    return ag.steuernummer13


def _hnr(hausnummer: str) -> tuple[str, str]:
    """ELSTER trennt Hausnummer (nur Ziffern) und Zusatz: „12a“ → („12“, „a“)."""
    m = re.fullmatch(r"\s*(\d{1,5})\s*(.*?)\s*", hausnummer or "")
    if not m:
        raise KontorError(f"Hausnummer „{hausnummer}“ lässt sich nicht in Nummer und Zusatz trennen.")
    return m.group(1), m.group(2)


def _hersteller(hersteller_id: str) -> str:
    if not re.fullmatch(r"\d{5}", hersteller_id or ""):
        raise KontorError("Hersteller-ID: fünf Ziffern erwartet.")
    return hersteller_id


# -- Lohnsteuer-Anmeldung ------------------------------------------------------------------------------------------

@dataclass
class Anteile:
    """Betrag einer Kennzahl nach Kalenderjahr aufgeteilt, in Cent. Vorjahr nur bei Zeitraum 01, 02, 41, 19,
    Folgejahr nur bei 12, 44, 19; sonst nur `lfd`. ELSTER verlangt die Aufteilung, sobald die Summe nicht null ist."""
    vorjahr: int = 0
    lfd: int = 0
    folgejahr: int = 0

    @property
    def summe(self) -> int:
        return self.vorjahr + self.lfd + self.folgejahr


def _anteile(wert: "int | Anteile") -> Anteile:
    return wert if isinstance(wert, Anteile) else Anteile(lfd=wert)


@dataclass
class LStA:
    """Eine Lohnsteuer-Anmeldung (Vordruck, Kennzahlen laut ERiC-Plausi-Dokumentation LStA 2026). Beträge in Cent.
    Felder, die null sind, bleiben im Datensatz weg (sonst Hinweise der Plausiprüfung); immer gesetzt werden Kz42, Kz48,
    Kz49, Kz83 und Kz86. Kz48 und Kz83 rechnet der Generator selbst (`kz48`, `kz83`). Eine berichtigte Anmeldung (Kz10)
    enthält wieder alle Werte, nicht nur die Änderung."""
    jahr: int
    zeitraum: str                      # "01".."12" Monat, "41".."44" Kalendervierteljahr, "19" Kalenderjahr
    arbeitnehmer: int                  # Kz86 Zahl der Arbeitnehmer
    kz42: "int | Anteile" = 0          # Summe der einzubehaltenden Lohnsteuer (laut Lohnsteuertabelle, ohne pauschale)
    kz49: "int | Anteile" = 0          # Solidaritätszuschlag
    kz61: "int | Anteile" = 0          # Evangelische Kirchensteuer
    kz62: "int | Anteile" = 0          # Römisch-Katholische Kirchensteuer
    kz63: "int | Anteile" = 0          # Alt-Katholische Kirchensteuer
    kz64: "int | Anteile" = 0          # Israelitische/Jüdische Kultussteuer (je nach Land)
    kz65: "int | Anteile" = 0          # Freireligiöse Gemeinde Mainz
    kz66: "int | Anteile" = 0          # Freireligiöse Gemeinde Offenbach am Main
    kz67: "int | Anteile" = 0          # Freireligiöse Landesgemeinde Baden
    kz68: "int | Anteile" = 0          # Freireligiöse Landesgemeinde Pfalz (RP) bzw. Arbeitnehmerkammer (HB)
    kz72: "int | Anteile" = 0          # Freie Religionsgemeinschaft Alzey
    kz73: "int | Anteile" = 0          # Israelitische Religionsgemeinschaft Württembergs
    kz74: "int | Anteile" = 0          # Israelitische Kultussteuer der kultussteuerberechtigten Gemeinden
    kz78: "int | Anteile" = 0          # Israelitische Religionsgemeinschaft Baden
    kz70: int = 0                      # Beiträge zur Arbeitskammer (Saarland), ohne Aufteilung nach Jahren
    kz41: int = 0                      # Summe der pauschalen Lohnsteuer ohne § 37b EStG
    kz44: int = 0                      # Summe der pauschalen Lohnsteuer nach § 37b EStG
    kz47: int = 0                      # pauschale Kirchensteuer im vereinfachten Verfahren
    kz33: int = 0                      # abzüglich Kürzungsbetrag für Besatzungsmitglieder von Handelsschiffen
    kz45: int = 0                      # abzüglich Förderbetrag zur betrieblichen Altersversorgung (§ 100 EStG)
    kz90: int = 0                      # Zahl der Arbeitnehmer mit BAV-Förderbetrag (Anzahl, kein Betrag); mit Kz45 gemeinsam
    kz21: bool = False                 # Haftungserklärung nach § 19a Abs. 4a Satz 1 EStG (Vermögensbeteiligungen)
    kz23: str = ""                     # weitere oder abweichende Angaben: Text (höchstens 180 Zeichen), setzt Kz23 = 1
    kz26: bool = False                 # SEPA-Lastschriftmandat für diesen Zeitraum ausnahmsweise widerrufen
    kz29: bool = False                 # Verrechnung des Erstattungsbetrags erwünscht / abgetreten
    kz92: bool = False                 # negativer Gesamtbetrag aufgrund Lohnsteuerjahresausgleich (nur ab 2026)
    berichtigt: bool = False           # Kz10 berichtigte Anmeldung
    erstellt: date | None = None       # Erstellungsdatum, Standard heute

    def _summe(self, *namen: str) -> int:
        return sum(_anteile(getattr(self, n)).summe for n in namen)

    @property
    def kz48(self) -> int:
        """Verbleiben: Kz41 + Kz42 + Kz44 - Kz33 - Kz45."""
        return self.kz41 + _anteile(self.kz42).summe + self.kz44 - self.kz33 - self.kz45

    @property
    def kz83(self) -> int:
        """Gesamtbetrag: Kz47 + Kz48 + Kz49 + alle Kirchensteuern Kz61..Kz78 + Kz70."""
        return (self.kz47 + self.kz48 + self.kz70
                + self._summe("kz49", "kz61", "kz62", "kz63", "kz64", "kz65", "kz66", "kz67", "kz68", "kz72", "kz73",
                              "kz74", "kz78"))


_ANTEIL_KZ = ("kz42", "kz49", "kz61", "kz62", "kz63", "kz64", "kz65", "kz66", "kz67", "kz68", "kz72", "kz73", "kz74",
              "kz78")
_VORJAHR_OK = {"01", "02", "41", "19"}
_FOLGEJAHR_OK = {"12", "44", "19"}
_ZEITRAEUME = {f"{m:02d}" for m in range(1, 13)} | {"19", "41", "42", "43", "44"}


def lsta_datenart(jahr: int) -> str:
    if jahr not in LSTA_JAHRE:
        raise KontorError(f"Lohnsteuer-Anmeldung {jahr}: Schema nicht hinterlegt (vorhanden: {', '.join(map(str, LSTA_JAHRE))}).")
    return f"LStA_{jahr}"


def _kz_zeilen(a: LStA) -> list[str]:
    """Die Kennzahlen in der Reihenfolge des Schemas."""
    if a.zeitraum not in _ZEITRAEUME:
        raise KontorError(f"Zeitraum „{a.zeitraum}“: erlaubt sind 01-12, 41-44 und 19.")
    if a.kz92 and a.jahr < 2026:
        raise KontorError("Kz92 gibt es erst im Schema 2026.")
    teile = {n: _anteile(getattr(a, n)) for n in _ANTEIL_KZ}
    for n, t in teile.items():
        if t.vorjahr and a.zeitraum not in _VORJAHR_OK:
            raise KontorError(f"{n.upper()}: Anteil Vorjahr ist im Zeitraum {a.zeitraum} nicht zulässig.")
        if t.folgejahr and a.zeitraum not in _FOLGEJAHR_OK:
            raise KontorError(f"{n.upper()}: Anteil Folgejahr ist im Zeitraum {a.zeitraum} nicht zulässig.")
    if bool(a.kz45) != bool(a.kz90):
        raise KontorError("Kz45 und Kz90 gehören zusammen: beide angeben oder keins.")
    if a.arbeitnehmer < 0 or a.kz90 < 0 or a.arbeitnehmer > 9999999:
        raise KontorError("Zahl der Arbeitnehmer: ungültig.")
    k: list[str] = []

    def add(name: str, wert: str) -> None:
        k.append(f"<{name}>{wert}</{name}>")

    def flag(name: str, an: bool) -> None:
        if an:
            add(name, "1")

    def betrag(name: str, cents: int) -> None:
        if cents:
            add(name, _eur(cents))

    def anteil(n: str, immer: bool = False) -> None:
        t = teile[n]
        kz = n.upper().replace("KZ", "Kz")
        if t.summe == 0 and not any((t.vorjahr, t.lfd, t.folgejahr)) and not immer:
            return
        for suffix, v in (("_Vorjahr", t.vorjahr), ("_lfdJahr", t.lfd), ("_Folgejahr", t.folgejahr)):
            if v:
                add(kz + suffix, _eur(v))
        add(kz, _eur(t.summe))

    flag("Kz10", a.berichtigt)
    flag("Kz21", a.kz21)
    if a.kz23:
        add("Kz23", "1")
        add("Kz23_Begruendung", _text("Kz23", a.kz23, 180))
    flag("Kz26", a.kz26)
    flag("Kz29", a.kz29)
    betrag("Kz33", a.kz33)
    betrag("Kz41", a.kz41)
    anteil("kz42", immer=True)
    betrag("Kz44", a.kz44)
    betrag("Kz45", a.kz45)
    betrag("Kz47", a.kz47)
    add("Kz48", _eur(a.kz48))
    anteil("kz49", immer=True)
    for n in ("kz61", "kz62", "kz63", "kz64", "kz65", "kz66", "kz67", "kz68"):
        anteil(n)
    betrag("Kz70", a.kz70)
    for n in ("kz72", "kz73", "kz74", "kz78"):
        anteil(n)
    add("Kz83", _eur(a.kz83))
    add("Kz86", str(a.arbeitnehmer))
    if a.kz90:
        add("Kz90", str(a.kz90))
    flag("Kz92", a.kz92)
    return k


def lsta(a: LStA, ag: Arbeitgeber, hersteller_id: str, test: bool, datenlieferant: str, version: str = "0.1") -> str:
    """Der vollständige ELSTER-Datensatz (Verfahren ElsterAnmeldung, Datenart LStA, Vorgang send-Auth)."""
    lsta_datenart(a.jahr)
    st, hid = _stnr(ag), _hersteller(hersteller_id)
    hnr, zusatz = _hnr(ag.hausnummer)
    strasse_komplett = f"{ag.strasse.strip()} {ag.hausnummer.strip()}"
    erstellt = (a.erstellt or date.today()).strftime("%Y%m%d")
    testmerker = f"<Testmerker>{TESTMERKER_LSTA}</Testmerker>" if test else ""
    lieferant = escape(datenlieferant)
    name = _text("Arbeitgeber Name", ag.name, 45)
    plz = _text("Arbeitgeber PLZ", ag.plz, 5)
    adresse = (f"<Bezeichnung>{_text('Arbeitgeber Name', ag.name, 50)}</Bezeichnung>"
               f"<Str>{_text('Arbeitgeber Straße', ag.strasse, 33)}</Str><Hausnummer>{hnr}</Hausnummer>"
               + (f"<HNrZusatz>{_text('Hausnummernzusatz', zusatz, 15)}</HNrZusatz>" if zusatz else "")
               + f"<Ort>{_text('Arbeitgeber Ort', ag.ort, 34)}</Ort><PLZ>{plz}</PLZ>"
               + (f"<Telefon>{_text('Telefon', ag.telefon, 20)}</Telefon>" if ag.telefon else "")
               + (f"<Email>{_text('E-Mail', ag.email, 70)}</Email>" if ag.email else ""))
    liefer_tel = (f"<Telefon>{_text('Telefon', ag.telefon, 20)}</Telefon>" if ag.telefon else "") \
        + (f"<Email>{_text('E-Mail', ag.email, 70)}</Email>" if ag.email else "")
    kennzahlen = "\n".join(_kz_zeilen(a))
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Elster xmlns="http://www.elster.de/elsterxml/schema/v11">
<TransferHeader version="11">
<Verfahren>ElsterAnmeldung</Verfahren><DatenArt>LStA</DatenArt><Vorgang>send-Auth</Vorgang>{testmerker}
<HerstellerID>{hid}</HerstellerID><DatenLieferant>{lieferant}</DatenLieferant>
<Datei><Verschluesselung>CMSEncryptedData</Verschluesselung><Kompression>GZIP</Kompression><TransportSchluessel/></Datei>
<VersionClient>Kontor {escape(version)}</VersionClient>
</TransferHeader>
<DatenTeil><Nutzdatenblock>
<NutzdatenHeader version="11"><NutzdatenTicket>{a.jahr}0001</NutzdatenTicket><Empfaenger id="F">{st[:4]}</Empfaenger>
<Hersteller><ProduktName>Kontor</ProduktName><ProduktVersion>{escape(version)}</ProduktVersion></Hersteller>
<DatenLieferant>{lieferant}</DatenLieferant></NutzdatenHeader>
<Nutzdaten>
<Anmeldungssteuern xmlns="http://finkonsens.de/elster/elsteranmeldung/lsta/v{a.jahr}" version="{a.jahr}">
<Erstellungsdatum>{erstellt}</Erstellungsdatum>
<DatenLieferant><Name>{name}</Name><Strasse>{_text('Arbeitgeber Straße mit Nummer', strasse_komplett, 30)}</Strasse>\
<PLZ>{plz}</PLZ><Ort>{_text('Arbeitgeber Ort', ag.ort, 30)}</Ort>{liefer_tel}</DatenLieferant>
<Steuerfall>
<Unternehmer>{adresse}</Unternehmer>
<Lohnsteueranmeldung>
<Jahr>{a.jahr}</Jahr><Zeitraum>{a.zeitraum}</Zeitraum><Steuernummer>{st}</Steuernummer><Kz09>{hid}</Kz09>
{kennzahlen}
</Lohnsteueranmeldung>
</Steuerfall>
</Anmeldungssteuern>
</Nutzdaten>
</Nutzdatenblock></DatenTeil>
</Elster>
"""


# -- Lohnsteuerbescheinigung ---------------------------------------------------------------------------------------

@dataclass
class Freibetrag:
    """Steuerfreibetrag oder Hinzurechnungsbetrag aus ELStAM, in Cent. Jahr und Monat in ganzen Euro (ELSTER verlangt
    ganze Zahlen), Woche und Tag mit Cent. Nur `jahr` ist Pflicht."""
    jahr: int
    monat: int | None = None
    woche: int | None = None
    tag: int | None = None


@dataclass
class Besteuerung:
    """Besteuerungsmerkmale ab einem Stichtag (ein ELStAM-Zeitraum; bei jeder Änderung beginnt ein neuer, höchstens 24)."""
    ab: date
    steuerklasse: int                  # 0..6; 0 nur in Sonderfällen (z. B. Schweizer Grenzgänger)
    faktor: str = ""                   # nur Klasse 4, z. B. "0.789"
    kinder: str = "0.0"                # Zahl der Kinderfreibeträge in 0,5-Schritten, z. B. "1.5"
    konfession: str = ""               # Kürzel des ELStAM-Katalogs: "ev", "rk", "ak" ...; leer = kein Kirchensteuerabzug
    ehegkonfession: str = ""           # nur bei abweichender Konfession des Ehegatten
    freibetrag: Freibetrag | None = None
    hinzurechnung: Freibetrag | None = None
    beitr_pkv: int = 0                 # nur ab 2026 (BeitrPkv): KV-Zuschuss + PV-Zuschuss aus dem KV/PV-Block der ELStAM
    beitr_pkv_basis: int = 0           # nur ab 2026 (BeitrPkvBasis): KV-Vorsorge + PV-Vorsorge aus dem KV/PV-Block


@dataclass
class VBez:
    """Nr. 8: in Nr. 3 enthaltene Versorgungsbezüge (je Jahr der Rechtsgrundlage ein Eintrag, höchstens 12)."""
    jahr: int                          # Kalenderjahr, in dem der Versorgungsbezug begann
    betrag: int
    bmg_freibetrag: int                # Bemessungsgrundlage für den Versorgungsfreibetrag
    beginn: str = ""                   # Monat "01".."12", nur bei zeitweiser Gewährung
    ende: str = ""
    einmalig: int | None = None        # einmalige Versorgungsbezüge, z. B. Sterbegeld


@dataclass
class JahrBetrag:
    jahr: int
    betrag: int


@dataclass
class LStB:
    """Eine Lohnsteuerbescheinigung für einen Arbeitnehmer und ein Lohnjahr (Schema lstb202501). Beträge in Cent;
    Nummern in den Kommentaren sind die Zeilen des Ausdrucks."""
    jahr: int
    dienst_von: date                   # Dauer des Dienstverhältnisses (Beginn und Ende im Lohnjahr, ohne Jahr gespeichert)
    dienst_bis: date
    idnr: str                          # Steuer-IdNr. des Arbeitnehmers, 11 Ziffern
    ordnungsmerkmal: str               # Personalnummer o. Ä., 1 bis 32 Zeichen
    name: str
    vorname: str
    geburtsdatum: date
    strasse: str
    hausnummer: str
    plz: str
    ort: str
    merkmale: list[Besteuerung]        # mindestens ein ELStAM-Zeitraum
    brutto: int                        # 3 Bruttoarbeitslohn einschließlich Sachbezüge
    lst: int                           # 4 einbehaltene Lohnsteuer von 3
    soli: int                          # 5 einbehaltener Solidaritätszuschlag von 3
    kist_an: int = 0                   # 6 einbehaltene Kirchensteuer des Arbeitnehmers von 3
    kist_ehegatte: int = 0             # 7 einbehaltene Kirchensteuer des Ehegatten (nur konfessionsverschiedene Ehe)
    namensvorsatz: str = ""
    namenszusatz: str = ""
    titel: str = ""
    geburtsname: str = ""
    geschlecht: str = ""               # M, W, D oder X; optional
    unterbrechungen: int | None = None  # 2 Zeiträume ohne Anspruch auf Arbeitslohn (Anzahl)
    versorgungsbezuege: list[VBez] = field(default_factory=list)                # 8
    ermst_versorgung: list[JahrBetrag] = field(default_factory=list)            # 9 Versorgungsbezüge für mehrere Jahre
    ermst_betrag: int | None = None    # 10 Arbeitslohn für mehrere Kalenderjahre, Entschädigungen (in 3, ohne 9)
    leistungen_progvorbeh: int = 0     # 15 Leistungen mit Progressionsvorbehalt (z. B. Lohnersatzleistungen)
    kurzarbeitergeld: int = 0          # 15a (Saison-)Kurzarbeitergeld (in 15 enthalten)
    stfrei_dba: int = 0                # 16a steuerfreier Arbeitslohn nach Doppelbesteuerungsabkommen
    stfrei_ate: int = 0                # 16b steuerfreier Arbeitslohn nach Auslandstätigkeitserlass
    stfrei_fahrt_ag: int = 0           # 17 steuerfreie Arbeitgeberleistungen Wohnung–Arbeitsstätte
    pausch_fahrt_ag: int = 0           # 18 pauschal besteuerte Arbeitgeberleistungen Wohnung–Arbeitsstätte
    stfrei_verpflegung: int = 0        # 20 steuerfreie Verpflegungszuschüsse bei Auswärtstätigkeit
    stfrei_dopp_haushalt: int = 0      # 21 steuerfreie Arbeitgeberleistungen bei doppelter Haushaltsführung
    ag_rv: int = 0                     # 22a Arbeitgeberanteil gesetzliche Rentenversicherung
    ag_bv: int = 0                     # 22b Arbeitgeberanteil berufsständische Versorgungseinrichtungen
    an_rv: int = 0                     # 23a Arbeitnehmeranteil gesetzliche Rentenversicherung
    an_bv: int = 0                     # 23b Arbeitnehmeranteil berufsständische Versorgungseinrichtungen
    zuschuss_gkv: int = 0              # 24a steuerfreie Arbeitgeberzuschüsse gesetzliche Krankenversicherung
    zuschuss_pkv: int = 0              # 24b steuerfreie Arbeitgeberzuschüsse private Krankenversicherung
    zuschuss_gpv: int = 0              # 24c steuerfreie Arbeitgeberzuschüsse gesetzliche Pflegeversicherung
    an_kv: int = 0                     # 25 Arbeitnehmerbeiträge gesetzliche Krankenversicherung
    an_pv: int = 0                     # 26 Arbeitnehmerbeiträge soziale Pflegeversicherung
    an_av: int = 0                     # 27 Arbeitnehmerbeiträge Arbeitslosenversicherung
    pkv_beitraege: int = 0             # 28 Beiträge private Kranken- und Pflege-Pflichtversicherung
    dba_tuerkei: int = 0               # 34 Freibetrag DBA Türkei
    grossbuchstaben: str = ""          # S, M, F, FR1..FR3, mit Komma getrennt (höchstens vier)
    kammerbeitrag: dict[str, int] = field(default_factory=dict)  # "HB" Arbeitnehmerkammer Bremen, "SL" Arbeitskammer Saarland
    an_wb_umlage: int = 0              # Arbeitnehmeranteil Winterbeschäftigungsumlage
    ag_zusatzvers: int = 0             # steuerpflichtiger Arbeitgeberbeitrag zur Zusatzversorgungskasse
    an_zusatzvers: int = 0             # Arbeitnehmerbeitrag zur Zusatzversorgungskasse
    arbeitstage: int | None = None     # Anzahl der Arbeitstage
    stfrei_fahrtk_ausw: int = 0        # steuerfreie Fahrtkosten bei Auswärtstätigkeit
    nicht_ermst_versorgung: list[JahrBetrag] = field(default_factory=list)  # Versorgungsbezüge mehrere Jahre, nicht ermäßigt
    werte: dict[str, int] = field(default_factory=dict)          # freiwillig bescheinigte Werte (Name → Betrag), höchstens 10
    texte: dict[str, str] = field(default_factory=dict)          # freiwillig bescheinigte Texte, höchstens 10
    aktivrente: int = 0                # ab 2026: Steuerfreistellung nach § 3 Nr. 21 EStG, geht als Wert „SteuerfreibetragAktivrente“
    korrektur_nr: int = 1              # laufende Nummer der Korrektur, geht in die KmId
    ref_km_id: str = ""                # KmId der zu korrigierenden Übermittlung (Pflicht bei Korrektur)
    korrektur_grund: str = "Einfache Berichtigung"   # oder "Paragraph 41c"; seit 01.03.2026 Pflicht bei Korrekturen

    @property
    def zeitraum_ttmm(self) -> str:
        return self.dienst_von.strftime("%d%m") + self.dienst_bis.strftime("%d%m")


# Einfache Beträge in der Reihenfolge des Schemas: (Feld, Element), vor und nach den Kammerbeiträgen
_WERTE_A = [("leistungen_progvorbeh", "LeistungenProgVorbeh"), ("kurzarbeitergeld", "KurzArbGeld"),
            ("stfrei_dba", "StFreiArbLohnDBA"), ("stfrei_ate", "StFreiArbLohnATE"),
            ("stfrei_fahrt_ag", "StFreiArbgLeistg"), ("pausch_fahrt_ag", "PauschArbgLeistg"),
            ("stfrei_verpflegung", "StFreiVerpfleg"), ("stfrei_dopp_haushalt", "StFreiDopHaushalt"),
            ("ag_rv", "ArbgAnteilRenVers"), ("ag_bv", "ArbgAnteilBerufsVers"), ("an_rv", "ArbnAnteilRenVers"),
            ("an_bv", "ArbnAnteilBerufsVers"), ("zuschuss_gkv", "StFreiGeKrankVers"),
            ("zuschuss_pkv", "StFreiPrKrankVers"), ("zuschuss_gpv", "StFreiGePflegeVers"),
            ("an_kv", "ArbnAnteilKrankVers"), ("an_pv", "ArbnAnteilPflegVers"), ("an_av", "ArbnAnteilArblVers"),
            ("pkv_beitraege", "BeitrPrKrankVers"), ("dba_tuerkei", "FreibetragDbaTuerkei")]
_WERTE_B = [("an_wb_umlage", "ArbnAnteilWBUmlage"), ("ag_zusatzvers", "ArbgAnteilZusatzVers"),
            ("an_zusatzvers", "ArbnAnteilZusatzVers")]


def lstb_datenart(jahr: int) -> str:
    """datenartVersion für ERiC: Lohnjahr 2025 `ElsterLohn` (Datenart LStB), ab 2026 `ElsterKMV` (Datenart LSTMitteilung)."""
    if jahr in LSTB_JAHRE:
        return "ElsterLohn"
    if jahr in LSTMITTEILUNG_JAHRE:
        return "ElsterKMV"
    raise KontorError(f"Lohnsteuerbescheinigung {jahr}: Schema nicht hinterlegt (2025 ElsterLohn, 2026 ElsterKMV; "
                      "ab 2027 kommt die LST-Detailversion 3).")


def _sauber(s: str) -> str:
    return re.sub(r"[^\w\-]", "_", s)


def km_id(b: LStB, ag: Arbeitgeber, korrektur: bool = False) -> str:
    """Bundesweit eindeutige Kennung der Mitteilung; die Korrektur verweist mit `ref_km_id` auf die vorige."""
    art = f"KOR-{b.korrektur_nr:03d}" if korrektur else "NEU"
    kid = f"{b.jahr}-LST-{b.idnr}-{b.zeitraum_ttmm}-{_stnr(ag)}-{_sauber(b.ordnungsmerkmal)}-{art}"
    if len(kid) > 100:
        raise KontorError("KmId länger als 100 Zeichen: Ordnungsmerkmal kürzen.")
    return kid


def _wert(name: str, cents: int, immer: bool = False) -> str:
    return f"<{name}>{_eur(cents)}</{name}>" if cents or immer else ""


def _freibetrag(name: str, f: Freibetrag | None) -> str:
    if f is None:
        return ""
    def ganz(c: int, feld: str) -> str:
        if c % 100:
            raise KontorError(f"{name}: {feld} nur in ganzen Euro.")
        return str(c // 100)
    attrs = f'jahr="{ganz(f.jahr, "Jahresbetrag")}"'
    if f.monat is not None:
        attrs += f' monat="{ganz(f.monat, "Monatsbetrag")}"'
    if f.woche is not None:
        attrs += f' woche="{_eur(f.woche)}"'
    if f.tag is not None:
        attrs += f' tag="{_eur(f.tag)}"'
    return f"<{name} {attrs}/>"


def _merkmal(m: Besteuerung, jahr: int) -> str:
    if (m.beitr_pkv or m.beitr_pkv_basis) and jahr < 2026:
        raise KontorError("BeitrPkv und BeitrPkvBasis gibt es erst ab Lohnjahr 2026.")
    if not 0 <= m.steuerklasse <= 6:
        raise KontorError(f"Steuerklasse {m.steuerklasse}: erlaubt sind 0 bis 6.")
    if m.ab.year > jahr:
        raise KontorError("ELStAM-Zeitraum beginnt nach dem Lohnjahr.")
    faktor = ""
    if m.faktor:
        if m.steuerklasse != 4:
            raise KontorError("Faktor ist nur in Steuerklasse 4 zulässig.")
        fk = Decimal(m.faktor).quantize(Decimal("0.001"))
        if not Decimal("0.001") <= fk <= Decimal("0.999"):
            raise KontorError("Faktor: 0,001 bis 0,999.")
        faktor = f' faktor="{fk}"'
    kinder = Decimal(m.kinder).quantize(Decimal("0.1"))
    if kinder * 2 % 1 or not 0 <= kinder <= Decimal("99.5"):
        raise KontorError("Kinderfreibeträge: in 0,5-Schritten, höchstens 99,5.")
    kist = ""
    if m.konfession:
        kist = f"<Kirchensteuerabzug konfession={quoteattr(m.konfession)}"
        kist += f" ehegkonfession={quoteattr(m.ehegkonfession)}/>" if m.ehegkonfession else "/>"
    return (f'<ELStAM gueltig_ab="{m.ab.isoformat()}"><Steuerklasse{faktor}>{m.steuerklasse}</Steuerklasse>'
            f"<Kinder>{kinder}</Kinder>{kist}{_freibetrag('Steuerfreibetrag', m.freibetrag)}"
            f"{_freibetrag('Hinzurechnungsbetrag', m.hinzurechnung)}"
            f"{_wert('BeitrPkv', m.beitr_pkv)}{_wert('BeitrPkvBasis', m.beitr_pkv_basis)}</ELStAM>")


def _person(b: LStB) -> str:
    gb = b.geburtsname
    return (f'<Person{f" geschlecht={quoteattr(b.geschlecht)}" if b.geschlecht else ""}>'
            f"<Familienname><Name>{_text('Name', b.name, 35)}</Name><Vorname>{_text('Vorname', b.vorname, 35)}</Vorname>"
            + (f"<Namensvorsatz>{_text('Namensvorsatz', b.namensvorsatz, 20)}</Namensvorsatz>" if b.namensvorsatz else "")
            + (f"<Namenszusatz>{_text('Namenszusatz', b.namenszusatz, 20)}</Namenszusatz>" if b.namenszusatz else "")
            + (f"<Titel>{_text('Titel', b.titel, 20)}</Titel>" if b.titel else "")
            + "</Familienname>"
            + (f"<Geburtsname><Name>{_text('Geburtsname', gb, 35)}</Name></Geburtsname>" if gb else "")
            + f"<Geburtsdatum>{b.geburtsdatum:%Y%m%d}</Geburtsdatum>"
            + _adresse(b.strasse, b.hausnummer, b.plz, b.ort, "Arbeitnehmer") + "</Person>")


def _adresse(strasse: str, hausnummer: str, plz: str, ort: str, wer: str) -> str:
    hnr, zusatz = _hnr(hausnummer)
    return (f"<Adresse><Str>{_text(f'{wer} Straße', strasse, 33)}</Str><Hausnummer>{hnr}</Hausnummer>"
            + (f"<HNrZusatz>{_text('Hausnummernzusatz', zusatz, 15)}</HNrZusatz>" if zusatz else "")
            + f"<Ort>{_text(f'{wer} Ort', ort, 34)}</Ort><PLZ>{_text(f'{wer} PLZ', plz, 5)}</PLZ></Adresse>")


def _grundlagen(b: LStB, ag: Arbeitgeber | None) -> str:
    """Besteuerungsgrundlagen. Mit `ag` (Lohnjahr 2025) samt Arbeitgeberblock, ohne (ab 2026) ohne Nr. 28 und Arbeitgeber."""
    z: list[str] = []
    if b.unterbrechungen is not None:
        z.append(f"<AnzahlU>{b.unterbrechungen}</AnzahlU>")
    z += [_wert("BruttoArbLohn", b.brutto, True), _wert("LSteuer", b.lst, True), _wert("Soli", b.soli, True),
          _wert("ArbnKiSteuer", b.kist_an), _wert("EhegKiSteuer", b.kist_ehegatte)]
    for v in b.versorgungsbezuege[:12]:
        attrs = f'jahr="{v.jahr}"'
        attrs += f' beginn="{v.beginn}"' if v.beginn else ""
        attrs += f' ende="{v.ende}"' if v.ende else ""
        attrs += f' bmgfreibetrag="{_eur(v.bmg_freibetrag)}"'
        attrs += f' einmversbezug="{_eur(v.einmalig)}"' if v.einmalig is not None else ""
        z.append(f"<VBez {attrs}>{_eur(v.betrag)}</VBez>")
    z += [f'<ErmStVBezMKalJahr jahr="{j.jahr}">{_eur(j.betrag)}</ErmStVBezMKalJahr>' for j in b.ermst_versorgung[:12]]
    if b.ermst_betrag is not None:
        z.append(_wert("ErmStBetrMKalJahr", b.ermst_betrag, True))
    if ag is None and b.pkv_beitraege:
        raise KontorError("Nr. 28 (pkv_beitraege) entfällt ab Lohnjahr 2026: die Beiträge gehören als beitr_pkv und "
                          "beitr_pkv_basis in die Besteuerungsmerkmale.")
    z += [_wert(el, getattr(b, f)) for f, el in _WERTE_A if ag is not None or f != "pkv_beitraege"]
    if b.grossbuchstaben:
        z.append(f"<Grossbuchstaben>{_text('Großbuchstaben', b.grossbuchstaben, 20)}</Grossbuchstaben>")
    for land, cents in b.kammerbeitrag.items():
        if land not in ("HB", "SL"):
            raise KontorError("Kammerbeitrag: nur HB (Bremen) oder SL (Saarland).")
        z.append(f'<Kammerbeitrag bundesland="{land}">{_eur(cents)}</Kammerbeitrag>')
    z += [_wert(el, getattr(b, f)) for f, el in _WERTE_B]
    if b.arbeitstage is not None:
        z.append(f"<AnzahlArbTag>{b.arbeitstage}</AnzahlArbTag>")
    z.append(_wert("StFreiFahrtKAusw", b.stfrei_fahrtk_ausw))
    z += [f'<NErmStVBezMKalJahr jahr="{j.jahr}">{_eur(j.betrag)}</NErmStVBezMKalJahr>' for j in b.nicht_ermst_versorgung[:12]]
    werte = dict(b.werte)
    if b.aktivrente:
        if ag is not None:
            raise KontorError("Der Aktivrente-Freibetrag wird erst ab Lohnjahr 2026 bescheinigt.")
        if not 0 < b.aktivrente <= 2400000:
            raise KontorError("Aktivrente: Werte über 0 bis höchstens 24.000 Euro (2.000 Euro je Monat).")
        werte["SteuerfreibetragAktivrente"] = b.aktivrente
    z += [f"<Wert name={quoteattr(n[:60])}>{_eur(c)}</Wert>" for n, c in list(werte.items())[:10]]
    z += [f"<Text name={quoteattr(n[:60])}>{_text('Text', t, 200)}</Text>" for n, t in list(b.texte.items())[:10]]
    if ag is None:
        return "".join(z)
    st = _stnr(ag)
    z.append(f"<Arbeitgeber><ArbGName>{_text('Arbeitgeber Name', ag.name, 60)}</ArbGName><ArbGFANr>{st[:4]}</ArbGFANr>"
             f"<ArbGFAName>{_text('Name des Finanzamts', ag.finanzamt, 100)}</ArbGFAName><ArbGStNr>{st}</ArbGStNr>"
             + _adresse(ag.strasse, ag.hausnummer, ag.plz, ag.ort, "Arbeitgeber") + "</Arbeitgeber>")
    return "".join(z)


def _lohn_rahmen(ag: Arbeitgeber, hersteller_id: str, test: bool, datenlieferant: str, version: str, ticket: str,
                 nutzdaten: str) -> str:
    st, hid = _stnr(ag), _hersteller(hersteller_id)
    land = _LAND.get(st[:2])
    if not land:
        raise KontorError(f"Finanzamt {st[:4]}: Bundesland für die Lohnsteuerbescheinigung nicht bekannt.")
    testmerker = f"<Testmerker>{TESTMERKER_LSTB}</Testmerker>" if test else ""
    lieferant = escape(datenlieferant)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Elster xmlns="http://www.elster.de/elsterxml/schema/v11">
<TransferHeader version="11">
<Verfahren>ElsterLohn</Verfahren><DatenArt>LStB</DatenArt><Vorgang>send-Auth</Vorgang>{testmerker}
<Empfaenger id="L"><Ziel>CS</Ziel></Empfaenger>
<HerstellerID>{hid}</HerstellerID><DatenLieferant>{lieferant}</DatenLieferant>
<Datei><Verschluesselung>CMSEncryptedData</Verschluesselung><Kompression>GZIP</Kompression></Datei>
</TransferHeader>
<DatenTeil><Nutzdatenblock>
<NutzdatenHeader version="11"><NutzdatenTicket>{escape(ticket)}</NutzdatenTicket><Empfaenger id="L">{land}</Empfaenger>
<Hersteller><ProduktName>Kontor</ProduktName><ProduktVersion>{escape(version)}</ProduktVersion></Hersteller>
<DatenLieferant>{lieferant}</DatenLieferant></NutzdatenHeader>
<Nutzdaten>
{nutzdaten}
</Nutzdaten>
</Nutzdatenblock></DatenTeil>
</Elster>
"""


def lstb(b: LStB, ag: Arbeitgeber, hersteller_id: str, test: bool, datenlieferant: str, korrektur: bool = False,
         version: str = "0.1") -> str:
    """Der vollständige ELSTER-Datensatz (Verfahren ElsterLohn, Datenart LStB, Version 202501). Eine Korrektur ist eine
    neue Übermittlung mit allen Werten: Anweisung art="Korrektur", eigene KmId und `RefKmId` der vorigen Übermittlung.
    Das Schema kennt dafür keinen Delta-Weg; Stornieren eines ganzen Belegs geht mit `lstb_storno`.
    Ab Lohnjahr 2026 baut die Funktion die LSTMitteilung (`lst_mitteilung`)."""
    lstb_datenart(b.jahr)
    if b.jahr in LSTMITTEILUNG_JAHRE:
        return lst_mitteilung(b, ag, hersteller_id, test, datenlieferant, korrektur, version)
    if not re.fullmatch(r"\d{11}", b.idnr or ""):
        raise KontorError("IdNr: 11 Ziffern erwartet.")
    if not b.merkmale or len(b.merkmale) > 24:
        raise KontorError("Besteuerungsmerkmale: ein bis 24 ELStAM-Zeiträume nötig.")
    if not 1 <= len(b.ordnungsmerkmal or "") <= 32:
        raise KontorError("Ordnungsmerkmal: 1 bis 32 Zeichen.")
    if b.geschlecht and b.geschlecht not in "MWDXmwdx":
        raise KontorError("Geschlecht: M, W, D oder X.")
    if korrektur:
        if not b.ref_km_id:
            raise KontorError("Korrektur braucht die KmId der zu korrigierenden Übermittlung (ref_km_id).")
        if b.korrektur_grund not in ("Einfache Berichtigung", "Paragraph 41c"):
            raise KontorError("Korrekturgrund: „Einfache Berichtigung“ oder „Paragraph 41c“.")
        anweisung = (f'<Anweisung art="Korrektur"><KmId>{escape(km_id(b, ag, True))}</KmId>'
                     f"<RefKmId>{escape(b.ref_km_id)}</RefKmId><KorrekturGrund>{b.korrektur_grund}</KorrekturGrund></Anweisung>")
    else:
        anweisung = f'<Anweisung art="Neu"><KmId>{escape(km_id(b, ag))}</KmId></Anweisung>'
    nutz = (f'<Lohnsteuerbescheinigung xmlns="http://finkonsens.de/rms/elo/lstb/v202501" version="202501" art="ELStAM">'
            f"{anweisung}"
            f'<Dauer jahr="{b.jahr}"><Anfang>{b.dienst_von:%d%m}</Anfang><Ende>{b.dienst_bis:%d%m}</Ende></Dauer>'
            f"<Allgemein><IdNr>{b.idnr}</IdNr><Ordnungsmerkmal>{_text('Ordnungsmerkmal', b.ordnungsmerkmal, 32)}</Ordnungsmerkmal>"
            f"{_person(b)}</Allgemein>"
            f"<Besteuerungsmerkmale>{''.join(_merkmal(m, b.jahr) for m in b.merkmale)}</Besteuerungsmerkmale>"
            f"<Besteuerungsgrundlagen>{_grundlagen(b, ag)}</Besteuerungsgrundlagen></Lohnsteuerbescheinigung>")
    return _lohn_rahmen(ag, hersteller_id, test, datenlieferant, version, f"{b.jahr}{b.idnr[-4:]}", nutz)


def lstb_storno(b: LStB, ag: Arbeitgeber, hersteller_id: str, test: bool, datenlieferant: str,
                version: str = "0.1") -> str:
    """Stornierung einer übermittelten Lohnsteuerbescheinigung (Schema LStBStorno v2): `b.ref_km_id` ist die KmId der
    zuletzt übermittelten Fassung, Allgemein und Arbeitgeber wie dort."""
    lstb_datenart(b.jahr)
    if b.jahr in LSTMITTEILUNG_JAHRE:
        return lst_mitteilung_storno(b, ag, hersteller_id, test, datenlieferant, version)
    if not b.ref_km_id:
        raise KontorError("Storno braucht die KmId der zu stornierenden Übermittlung (ref_km_id).")
    st = _stnr(ag)
    nutz = (f'<LStBStorno xmlns="http://finkonsens.de/rms/elo/lstbstorno/v2" version="2">'
            f'<Anweisung art="Storno"><KmId>{escape(km_id(b, ag) .removesuffix("-NEU"))}-STO</KmId>'
            f"<RefKmId>{escape(b.ref_km_id)}</RefKmId></Anweisung><Zuflussjahr>{b.jahr}</Zuflussjahr>"
            f"<Allgemein><IdNr>{b.idnr}</IdNr><Ordnungsmerkmal>{_text('Ordnungsmerkmal', b.ordnungsmerkmal, 32)}</Ordnungsmerkmal></Allgemein>"
            f"<Arbeitgeber><ArbGName>{_text('Arbeitgeber Name', ag.name, 60)}</ArbGName><ArbGStNr>{st}</ArbGStNr></Arbeitgeber>"
            "</LStBStorno>")
    return _lohn_rahmen(ag, hersteller_id, test, datenlieferant, version, f"{b.jahr}{b.idnr[-4:]}S", nutz)


# -- LSTMitteilung (Lohnjahr ab 2026, Verfahren ElsterKMV) ---------------------------------------------------------
# KONSENS-Mitteilung (Kontrollmitteilung) Version 7 mit LST-Detailversion 2. Quelle: esteuer.de, KM7_LST_000002 und
# SST_KMV_KM7_LST_Version_2. ERiC prüft die Nutzdaten nicht (Schema und Fehler kommen erst im Protokoll, KMVProtokoll).

_KMV_BASE = "http://finkonsens.de/rms/kmv/base/v7"


def _kmv_adresse(strasse: str, hausnummer: str, plz: str, ort: str, wer: str, tag: str) -> str:
    hnr, zusatz = _hnr(hausnummer)
    return (f"<{tag}><StrAdresse><Hausnummer>{hnr}</Hausnummer>"
            + (f"<HNrZusatz>{_text('Hausnummernzusatz', zusatz, 20)}</HNrZusatz>" if zusatz else "")
            + f"<Ort>{_text(f'{wer} Ort', ort, 72)}</Ort><Str>{_text(f'{wer} Straße', strasse, 72)}</Str>"
            f"<PLZ>{_text(f'{wer} PLZ', plz, 5)}</PLZ></StrAdresse></{tag}>")


def _verfasser(ag: Arbeitgeber) -> str:
    st = _stnr(ag)
    komm = ""
    if ag.telefon or ag.email:
        komm = ("<Komm>" + (f"<FreiDefTel>{_text('Telefon', ag.telefon, 30)}</FreiDefTel>" if ag.telefon else "")
                + (f"<EMail>{_text('E-Mail', ag.email, 254)}</EMail>" if ag.email else "") + "</Komm>")
    return (f'<Verfasser xmlns="{_KMV_BASE}"><Name>{_text("Arbeitgeber Name", ag.name, 72)}</Name>'
            f'{_kmv_adresse(ag.strasse, ag.hausnummer, ag.plz, ag.ort, "Arbeitgeber", "VerfasserAdresse")}{komm}'
            f'<Ordnungsbegriff art="StNr">{st}</Ordnungsbegriff><VerfasserInfo typ="FANr">{st[:4]}</VerfasserInfo>'
            f'<VerfasserInfo typ="FAName">{_text("Name des Finanzamts", ag.finanzamt, 250)}</VerfasserInfo></Verfasser>')


def _kmv_rahmen(ag: Arbeitgeber, hersteller_id: str, test: bool, datenlieferant: str, version: str, ticket: str,
                nutzdaten: str) -> str:
    st, hid = _stnr(ag), _hersteller(hersteller_id)
    testmerker = f"<Testmerker>{TESTMERKER_KMV}</Testmerker>" if test else ""
    lieferant = escape(datenlieferant)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Elster xmlns="http://www.elster.de/elsterxml/schema/v11">
<TransferHeader version="11">
<Verfahren>ElsterKMV</Verfahren><DatenArt>LSTMitteilung</DatenArt><Vorgang>send-Auth</Vorgang>{testmerker}
<Empfaenger id="L"><Ziel>CS</Ziel></Empfaenger>
<HerstellerID>{hid}</HerstellerID><DatenLieferant>{lieferant}</DatenLieferant>
<Datei><Verschluesselung>CMSEncryptedData</Verschluesselung><Kompression>GZIP</Kompression></Datei>
</TransferHeader>
<DatenTeil><Nutzdatenblock>
<NutzdatenHeader version="11"><NutzdatenTicket>{escape(ticket)}</NutzdatenTicket><Empfaenger id="L">NW</Empfaenger>
<Hersteller><ProduktName>Kontor</ProduktName><ProduktVersion>{escape(version)}</ProduktVersion></Hersteller>
<DatenLieferant>{lieferant}</DatenLieferant></NutzdatenHeader>
<Nutzdaten>
{nutzdaten}
</Nutzdaten>
</Nutzdatenblock></DatenTeil>
</Elster>
"""


def _kmv_pruefen(b: LStB, ag: Arbeitgeber) -> None:
    if b.jahr not in LSTMITTEILUNG_JAHRE:
        lstb_datenart(b.jahr)
        raise KontorError(f"LSTMitteilung gilt ab Lohnjahr 2026, nicht {b.jahr}.")
    if not re.fullmatch(r"\d{11}", b.idnr or ""):
        raise KontorError("IdNr: 11 Ziffern erwartet.")
    if not b.merkmale or len(b.merkmale) > 12:
        raise KontorError("Besteuerungsmerkmale: ein bis 12 ELStAM-Zeiträume nötig.")
    if not 1 <= len(b.ordnungsmerkmal or "") <= 50:
        raise KontorError("Ordnungsmerkmal: 1 bis 50 Zeichen.")
    if b.geschlecht and b.geschlecht.lower() not in ("m", "w", "x", "d"):
        raise KontorError("Geschlecht: M, W, D oder X.")


def lst_mitteilung(b: LStB, ag: Arbeitgeber, hersteller_id: str, test: bool, datenlieferant: str,
                   korrektur: bool = False, version: str = "0.1") -> str:
    """Der vollständige ELSTER-Datensatz (Verfahren ElsterKMV, Datenart LSTMitteilung, Kontrollmitteilung 7, LST 2).
    Korrektur wie bei `lstb`: neue Mitteilung mit allen Werten, eigene KmId und RefKmId der vorigen. Nr. 28 gibt es
    nicht mehr; die privaten KV/PV-Beiträge stehen in den Besteuerungsmerkmalen (`Besteuerung.beitr_pkv`). Der Arbeitgeber
    ist jetzt der Verfasser (Name, Adresse, Steuernummer, Finanzamtsnummer und -name)."""
    _kmv_pruefen(b, ag)
    if korrektur:
        if not b.ref_km_id:
            raise KontorError("Korrektur braucht die KmId der zu korrigierenden Übermittlung (ref_km_id).")
        if b.korrektur_grund not in ("Einfache Berichtigung", "Paragraph 41c"):
            raise KontorError("Korrekturgrund: „Einfache Berichtigung“ oder „Paragraph 41c“.")
        anweisung = (f'<Anweisung art="Korrektur"><KmId>{escape(km_id(b, ag, True))}</KmId>'
                     f"<RefKmId>{escape(b.ref_km_id)}</RefKmId><KorrekturGrund>{b.korrektur_grund}</KorrekturGrund></Anweisung>")
    else:
        anweisung = f'<Anweisung art="Neu"><KmId>{escape(km_id(b, ag))}</KmId></Anweisung>'
    gb = b.geburtsname
    geb = (f'<Geburtsdaten gebDat="{b.geburtsdatum:%Y%m%d}"'
           + (f"><Geburtsname><Name>{_text('Geburtsname', gb, 72)}</Name></Geburtsname></Geburtsdaten>" if gb else "/>"))
    person = (f'<NatPers idNr="{b.idnr}"' + (f' geschlecht="{b.geschlecht.lower()}"' if b.geschlecht else "") + ">"
              f"<Name>{_text('Name', b.name, 72)}</Name><Vorname>{_text('Vorname', b.vorname, 72)}</Vorname>"
              + (f"<NamensVorsatz>{_text('Namensvorsatz', b.namensvorsatz, 25)}</NamensVorsatz>" if b.namensvorsatz else "")
              + (f"<NamensZusatz>{_text('Namenszusatz', b.namenszusatz, 60)}</NamensZusatz>" if b.namenszusatz else "")
              + (f"<Titel>{_text('Titel', b.titel, 30)}</Titel>" if b.titel else "") + f"{geb}</NatPers>")
    fall = (f'<BetroffenerFall xmlns="{_KMV_BASE}"><Zeitraum von="{b.dienst_von:%Y%m%d}" bis="{b.dienst_bis:%Y%m%d}"/>'
            f'<Steuerfall><Ordnungsbegriff art="AgOb">{_text("Ordnungsmerkmal", b.ordnungsmerkmal, 50)}</Ordnungsbegriff>'
            f'<BetroffenePerson>{person}'
            f'{_kmv_adresse(b.strasse, b.hausnummer, b.plz, b.ort, "Arbeitnehmer", "Adresse")}'
            "</BetroffenePerson></Steuerfall></BetroffenerFall>")
    lst = ('<LST xmlns="http://finkonsens.de/rms/kmv/lst/v2" version="2">'
           f"<Besteuerungsgrundlage>{_grundlagen(b, None)}</Besteuerungsgrundlage>"
           f"<Besteuerungsmerkmale>{''.join(_merkmal(m, b.jahr) for m in b.merkmale)}</Besteuerungsmerkmale></LST>")
    nutz = (f'<Kontrollmitteilung xmlns="http://finkonsens.de/rms/kmv/km/v7" version="7">{anweisung}{_verfasser(ag)}'
            f"{fall}{lst}</Kontrollmitteilung>")
    return _kmv_rahmen(ag, hersteller_id, test, datenlieferant, version, f"{b.jahr}{b.idnr[-4:]}", nutz)


def lst_mitteilung_storno(b: LStB, ag: Arbeitgeber, hersteller_id: str, test: bool, datenlieferant: str,
                          version: str = "0.1") -> str:
    """Stornierung einer LSTMitteilung (KMStorno v7): `b.ref_km_id` ist die KmId der zuletzt übermittelten Fassung.
    Zulässig nur, wenn die Grundlage entfallen ist oder die IdNr falsch war (dann Storno und neue Mitteilung)."""
    _kmv_pruefen(b, ag)
    if not b.ref_km_id:
        raise KontorError("Storno braucht die KmId der zu stornierenden Übermittlung (ref_km_id).")
    nutz = ('<KMStorno xmlns="http://finkonsens.de/rms/kmv/storno/v7" version="7" kmArt="LST">'
            f'<Anweisung art="Storno"><KmId>{escape(km_id(b, ag).removesuffix("-NEU"))}-STO</KmId>'
            f"<RefKmId>{escape(b.ref_km_id)}</RefKmId></Anweisung>{_verfasser(ag)}"
            f'<Zeitraum xmlns="{_KMV_BASE}" von="{b.dienst_von:%Y%m%d}" bis="{b.dienst_bis:%Y%m%d}"/></KMStorno>')
    return _kmv_rahmen(ag, hersteller_id, test, datenlieferant, version, f"{b.jahr}{b.idnr[-4:]}S", nutz)
