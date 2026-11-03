"""Kommandozeile für Claude. JSON rein (--json DATEI oder -), JSON raus, Fehler als JSON auf stderr mit Exit 1.
Abschließen, Stornieren, Monatsabschluss, Beleg bestätigen und löschen gibt es hier absichtlich nicht (Spec 6.2, 10)."""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

from .. import config
from ..config import ID_PATTERN, ConfigError, _checked, mandant, mandanten, register_path
from ..context import open_ctx
from ..core.errors import KontorError
from ..reports.euer import euer
from ..reports.export import journal_csv
from ..reports.overview import overview
from ..reports.ustva import ustva
from ..services import documents as docs
from ..services import finalize as fin
from ..services import integrity
from ..services import masterdata as md
from ..services import payments as pay
from ..services import receipts as rc

HOST = "127.0.0.1"
PORT = 8792


def _load(path: str | None) -> dict:
    if not path:
        return {}
    text = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    return json.loads(text)


def _doc(ctx, did: int) -> dict:
    d = docs.get_document(ctx, did)
    d.pop("snapshot_json", None)
    return d


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="kontor", description="Kontor: Rechnungen und Buchhaltung (Kommandozeile für Claude)")
    p.add_argument("--mandant", default=os.environ.get("KONTOR_MANDANT"),
                   help="Mandant aus dem Register (Pflicht; sonst KONTOR_MANDANT). Liste: kontor mandanten")
    sub = p.add_subparsers(dest="area", required=True)

    def area(name: str, help_: str):
        return sub.add_parser(name, help=help_).add_subparsers(dest="action", required=True)

    def with_json(parser, required=True):
        parser.add_argument("--json", required=required, help="JSON-Datei oder - für stdin")
        return parser

    e = area("einstellungen", "Stammdaten")
    e.add_parser("zeigen")
    with_json(e.add_parser("setzen"))
    e.add_parser("logo", help="Logo-Datei (SVG/PNG/JPG) in den Datenordner übernehmen").add_argument("datei")

    x = sub.add_parser("init", help="Datenordner anlegen und optional im Register eintragen (Einrichtung, ONBOARDING.md)")
    x.add_argument("--daten", required=True, help="Datenordner, außerhalb des Code-Repos")
    x.add_argument("--id", help="Mandanten-ID für das Register, z. B. meine-firma")
    x.add_argument("--name", help="Anzeigename im Register")
    x.add_argument("--farbe", default="#2F4858")
    x.add_argument("--bedingungen-gelesen", action="store_true",
                   help="TERMS.md und DISCLAIMER.md gelesen und einverstanden (einmal je Rechner, gespeichert im Zustandsordner)")
    sub.add_parser("einrichtung", help="was für den Mandanten noch fehlt (Einrichtung)")

    k = area("kunde", "Kunden")
    with_json(k.add_parser("anlegen"))
    k.add_parser("liste")
    k.add_parser("zeigen").add_argument("id", type=int)
    x = with_json(k.add_parser("aendern"))
    x.add_argument("id", type=int)

    pos = area("position", "Standardpositionen")
    with_json(pos.add_parser("anlegen"))
    pos.add_parser("liste").add_argument("--suche", default="")

    tv = area("textvorlage", "Textvorlagen")
    with_json(tv.add_parser("anlegen"))
    tv.add_parser("liste")

    a = area("angebot", "Angebote")
    with_json(a.add_parser("entwurf")).add_argument("--id", type=int)
    r = area("rechnung", "Rechnungen")
    with_json(r.add_parser("entwurf")).add_argument("--id", type=int)
    x = r.add_parser("aus-angebot")
    x.add_argument("id", type=int)
    g = x.add_mutually_exclusive_group()
    g.add_argument("--abschlag", help="Prozent, z. B. 50")
    g.add_argument("--schluss", action="store_true")

    d = area("dokument", "Dokumente lesen")
    x = d.add_parser("liste")
    x.add_argument("--gruppe", choices=["angebote", "rechnungen"])
    x.add_argument("--status")
    x.add_argument("--jahr", type=int)
    d.add_parser("zeigen").add_argument("id", type=int)
    x = d.add_parser("vorschau")
    x.add_argument("id", type=int)
    x.add_argument("--out", required=True)
    d.add_parser("loeschen", help="nur Entwürfe").add_argument("id", type=int)
    x = d.add_parser("status", help="Angebot angenommen/abgelehnt")
    x.add_argument("id", type=int)
    x.add_argument("neu", choices=["angenommen", "abgelehnt"])
    x = d.add_parser("als-position")
    x.add_argument("id", type=int)
    x.add_argument("position", type=int)

    z = area("zahlung", "Zahlungen")
    with_json(z.add_parser("erfassen"))
    with_json(z.add_parser("ust"))
    z.add_parser("liste").add_argument("--jahr", type=int)
    with_json(z.add_parser("ohne-rechnung", help="Zahlungseingang, zu dem die Rechnung noch fehlt (zählt sofort)"))
    x = z.add_parser("verknuepfen", help="abgeschlossene Rechnung übernimmt eine Zahlung ohne Rechnung")
    x.add_argument("id", type=int)
    x.add_argument("--dokument", required=True, type=int)

    b = area("beleg", "Belege")
    b.add_parser("erfassen").add_argument("datei")
    x = with_json(b.add_parser("vorschlag"))
    x.add_argument("id", type=int)
    b.add_parser("liste").add_argument("--status", choices=["eingang", "geprueft"])
    b.add_parser("zeigen").add_argument("id", type=int)
    with_json(b.add_parser("eigenbeleg", help="Ausgabe ohne Rechnung"))

    ein = area("eingang", "Telegram-Eingang")
    x = ein.add_parser("abholen")
    x.add_argument("--host", help="SSH-Host des Eingangs, sonst KONTOR_EINGANG_HOST")

    au = area("auswertung", "Auswertungen")
    au.add_parser("ustva").add_argument("zeitraum", help="JJJJ-MM oder JJJJ-Qn")
    au.add_parser("euer").add_argument("jahr", type=int)
    au.add_parser("uebersicht")
    bg = au.add_parser("bilanz", help="Bilanz und GuV aus dem Journal (Spec 18.8)")
    bg.add_argument("jahr", type=int)
    bg.add_argument("--stichtag", help="JJJJ-MM-TT, Standard 31.12.")
    au.add_parser("auffaelligkeiten", help="bekannte Buchungsfallen im Journal").add_argument("jahr", type=int)

    bk = area("bank", "Bankumsätze als Quelle der Buchungen (Spec 18.7a)")
    x = bk.add_parser("konto", help="Bankkonto anlegen oder ändern")
    x.add_argument("--iban", required=True)
    x.add_argument("--name", required=True)
    x.add_argument("--konto", required=True, help="SKR04-Bankkonto, z. B. 1813")
    bk.add_parser("konten")
    bk.add_parser("import", help="DATEV-Kontoumsatzexport (Lexware Banking)").add_argument("--datei", required=True)
    x = bk.add_parser("liste")
    x.add_argument("--jahr", type=int)
    x.add_argument("--offen", action="store_true", help="nur Umsätze ohne Zuordnung")
    x = bk.add_parser("zuordnen", help="ein Umsatz, ein Ziel")
    x.add_argument("id", type=int)
    x.add_argument("--art", required=True, choices=["rechnung", "konto", "intern", "beleg"])
    x.add_argument("--nummer", action="append", help="Rechnungsnummer, z. B. RE-2026-0042; mehrfach = Sammelzahlung")
    x.add_argument("--konto", default="")
    x.add_argument("--steuer", default="", choices=["", "19", "7", "0", "rc_eu", "rc_drittland"])
    x.add_argument("--beleg", type=int)
    x.add_argument("--notiz", default="")
    x = bk.add_parser("loesen")
    x.add_argument("id", type=int)
    x.add_argument("--grund", required=True)
    x = bk.add_parser("dublette-echt", help="markierte Dublette ist laut Kontoauszug echt")
    x.add_argument("id", type=int)
    x.add_argument("--grund", required=True)
    x = bk.add_parser("regel")
    x.add_argument("--muster", required=True)
    x.add_argument("--konto", required=True)
    x.add_argument("--steuer", required=True, choices=["19", "7", "0", "rc_eu", "rc_drittland"])
    bk.add_parser("regeln-lernen", help="aus einem Lexware-Kontenexport (Datei wird nur gelesen)").add_argument(
        "--datei", required=True)
    bk.add_parser("vorschlaege", help="Regeln und Rechnungsnummern auf offene Umsätze anwenden").add_argument(
        "--jahr", required=True, type=int)
    x = bk.add_parser("anker", help="Endsaldo laut Kontoauszug")
    x.add_argument("--iban", required=True)
    x.add_argument("--datum", required=True)
    x.add_argument("--saldo", required=True)
    x.add_argument("--notiz", default="")

    lo = area("lohn", "Lohn (nur mit Profilschalter lohn); Senden nur über Aufträge zur Freigabe")
    x = lo.add_parser("mitarbeiter", help="ohne --json die Liste; mit --json anlegen bzw. mit --id ändern")
    x.add_argument("--id", type=int)
    with_json(x, required=False)
    lo.add_parser("zeigen", help="Mitarbeiter mit Merkmalen, Bezügen, Vortrag").add_argument("id", type=int)
    x = with_json(lo.add_parser("merkmale", help="neue Fassung der Merkmale ab Monat (steuerklasse, beitragsgruppe, …)"))
    x.add_argument("id", type=int)
    x.add_argument("--ab", required=True, help="JJJJ-MM")
    x = with_json(lo.add_parser("bezug", help="Bezug oder Abzug (lohnart, betrag, ab/bis oder monat, bezeichnung)"))
    x.add_argument("id", type=int)
    x = lo.add_parser("bezug-beenden")
    x.add_argument("bezug", type=int)
    x.add_argument("--bis", required=True, help="letzter Monat JJJJ-MM")
    x = with_json(lo.add_parser("vortrag", help="Jahreswerte aus einem anderen Programm bis einschließlich --bis"))
    x.add_argument("id", type=int)
    x.add_argument("--jahr", required=True, type=int)
    x.add_argument("--bis", required=True)
    x.add_argument("--notiz", default="")
    x = lo.add_parser("abrechnen", help="Abrechnung als Entwurf rechnen (speichert nichts); ohne --id alle des Monats")
    x.add_argument("--monat", required=True)
    x.add_argument("--id", type=int)
    x = lo.add_parser("journal", help="Lohnjournal")
    x.add_argument("--jahr", required=True, type=int)
    x.add_argument("--monat")
    x = lo.add_parser("lohnkonto")
    x.add_argument("id", type=int)
    x.add_argument("--jahr", required=True, type=int)
    lo.add_parser("kosten", help="Lohnkosten des Arbeitgebers").add_argument("--jahr", required=True, type=int)
    lo.add_parser("kassen", help="Beiträge je Kasse (Beitragsnachweis)").add_argument("--monat", required=True)
    x = lo.add_parser("lsta", help="Lohnsteuer-Anmeldung: Werte, mit --pruefen lokal durch ERiC")
    x.add_argument("--jahr", required=True, type=int)
    x.add_argument("--zeitraum", help="01–12, 41–44 oder 19; ohne alle Zeiträume des Jahres")
    x.add_argument("--pruefen", action="store_true")
    x = lo.add_parser("lstb", help="Lohnsteuerbescheinigung: Werte, mit --pruefen durch ERiC")
    x.add_argument("id", type=int)
    x.add_argument("--jahr", required=True, type=int)
    x.add_argument("--pruefen", action="store_true")
    lo.add_parser("auffaelligkeiten").add_argument("--jahr", required=True, type=int)
    x = lo.add_parser("festschreiben", help="Abrechnung festschreiben; mit --bis alle offenen Monate bis --monat")
    x.add_argument("id", type=int)
    x.add_argument("--monat", required=True)
    x.add_argument("--bis", action="store_true")
    x = lo.add_parser("stornieren", help="festgeschriebene Abrechnung stornieren (nur die jüngste)")
    x.add_argument("abrechnung", type=int)
    x.add_argument("--grund", required=True)
    x = lo.add_parser("lsta-extern", help="vermerken, dass ein anderes Programm die Anmeldung schon gesendet hat")
    x.add_argument("--jahr", required=True, type=int)
    x.add_argument("--zeitraum", required=True)
    x.add_argument("--notiz", required=True)
    x = lo.add_parser("elstam", help="empfangene ELStAM; mit --uebernehmen ID in die Merkmale, mit --pruefen eine Anfrage")
    x.add_argument("--offen", action="store_true")
    x.add_argument("--uebernehmen", type=int)
    x.add_argument("--pruefen", choices=["anmelden", "wechsel", "abmelden"])
    x.add_argument("--id", type=int, help="Mitarbeiter (mit --pruefen)")
    x.add_argument("--referenzdatum")

    au = area("auftrag", "Aufträge zur Freigabe: vorbereiten hier, senden nur der Mensch unter „Freigaben“")
    x = au.add_parser("liste")
    x.add_argument("--status", choices=["offen", "gesendet", "abgelehnt", "veraltet", "verworfen"])
    for name, hilfe in (("lsta", "Lohnsteuer-Anmeldung"), ("lstb", "Lohnsteuerbescheinigung"), ("elstam", "ELStAM-Anfrage"),
                        ("abholen", "ELStAM aus dem Postfach abholen"), ("ebilanz", "E-Bilanz"),
                        ("ustva", "Umsatzsteuer-Voranmeldung (Zeitraum JJJJ-MM oder JJJJ-Qn)")):
        x = au.add_parser(name, help=hilfe)
        x.add_argument("--test", action="store_true", help="Testversand")
        x.add_argument("--notiz", default="")
        if name in ("lsta", "lstb", "ebilanz"):
            x.add_argument("--jahr", required=True, type=int)
        if name in ("lsta", "ustva"):
            x.add_argument("--zeitraum", required=True)
        if name in ("lstb", "elstam"):
            x.add_argument("--mitarbeiter", required=True, type=int)
        if name == "elstam":
            x.add_argument("--vorgang", required=True, choices=["anmelden", "wechsel", "abmelden"])
            x.add_argument("--referenzdatum")
    x = au.add_parser("verwerfen")
    x.add_argument("id", type=int)
    x.add_argument("--grund", required=True)

    bt = area("btc", "Bitcoin-Nebenbuch (Spec 18.14, nur mit Profilschalter bitcoin)")
    x = bt.add_parser("wallet", help="Wallet anlegen oder ändern; ohne --name die Liste")
    x.add_argument("--name")
    x.add_argument("--art", default="wallet", choices=["wallet", "boerse"])
    x.add_argument("--inaktiv", action="store_true")
    x = bt.add_parser("import", help="Export als Entwürfe übernehmen (Adressen und Transaktions-IDs werden verworfen)")
    x.add_argument("--format", required=True, choices=["kontor", "bitbox", "coinfinity"])
    x.add_argument("--datei", required=True)
    x.add_argument("--wallet", help="Pflicht für bitbox und coinfinity")
    with_json(bt.add_parser("erfassen", help="Bewegung von Hand (wallet, art, datum, sats, eur, …)"))
    x = bt.add_parser("liste", help="Bewegungen, Entwürfe mit Vorschlag")
    x.add_argument("--jahr", type=int)
    x.add_argument("--status", choices=["entwurf", "bestaetigt", "verworfen"])
    x = bt.add_parser("bestaetigen", help="Entwurf bestätigen (erst dann wird gebucht); --zurueck setzt zurück")
    x.add_argument("id", type=int)
    x.add_argument("--zurueck", action="store_true")
    x.add_argument("--grund", default="")
    x = bt.add_parser("zuordnen", help="Entwurf klassifizieren, Vorschlag übernehmen, Bankumsatz zuordnen oder verwerfen")
    x.add_argument("id", type=int)
    x.add_argument("--art", choices=["kauf", "verkauf", "kundenzahlung", "zahlung", "umbuchung", "verlust"])
    x.add_argument("--rechnung", help="Rechnungsnummer (Kundenzahlung)")
    x.add_argument("--beleg", type=int, help="Eingangsbeleg (Zahlung)")
    x.add_argument("--konto", help="Aufwandskonto (Zahlung einer Eingangsrechnung)")
    x.add_argument("--steuer", choices=["19", "7", "0", "rc_eu", "rc_drittland"], help="Steuerschlüssel (Zahlung)")
    x.add_argument("--ziel", help="Ziel-Wallet (Umbuchung)")
    x.add_argument("--eur", help="Kaufbetrag, Vertragsbetrag brutto oder Nennwert")
    x.add_argument("--gebuehr-eur")
    x.add_argument("--gebuehr-sats", type=int)
    x.add_argument("--vertragsdatum")
    x.add_argument("--ak", help="Anschaffungskosten einer Kundenzahlung (Marktwert statt Nennwert)")
    x.add_argument("--notiz")
    x.add_argument("--vorschlag", action="store_true", help="Vorschlag (offene Rechnung) übernehmen")
    x.add_argument("--bank", action="store_true", help="passenden Bankumsatz auf das Verrechnungskonto zuordnen")
    x.add_argument("--verwerfen", action="store_true", help="Entwurf verwerfen (mit --grund)")
    x.add_argument("--grund", default="")
    x = bt.add_parser("saldo", help="Saldo laut Wallet eintragen; ohne --wallet die Liste mit Differenzen")
    x.add_argument("--wallet")
    x.add_argument("--datum")
    x.add_argument("--sats", type=int)
    x.add_argument("--notiz", default="")
    x = bt.add_parser("kurse", help="Tageskurse: --abrufen (Kraken, Lücken aus Bitstamp) oder --datum/--eur von Hand; "
                                    "ohne Schalter der Stand")
    x.add_argument("--abrufen", action="store_true")
    x.add_argument("--ab", help="Bitstamp-Historie ab diesem Tag (JJJJ-MM-TT), z. B. vor dem Kraken-Fenster")
    x.add_argument("--datum")
    x.add_argument("--eur", help="EUR je BTC")
    x = bt.add_parser("bewertung", help="Bewertung des Jahres; mit --vortrag der Anfangsbestand einer Wallet")
    x.add_argument("--jahr", type=int, required=True)
    x.add_argument("--vortrag", metavar="WALLET")
    x.add_argument("--sats", type=int)
    x.add_argument("--ak")
    x.add_argument("--buchwert")

    jn = area("journal", "Journal der Bilanz-Mandanten (Spec 18.8a)")
    x = jn.add_parser("eroeffnung", help="Vortrag zum 01.01. aus der Saldenliste des Vorjahres")
    x.add_argument("--jahr", required=True, type=int)
    x.add_argument("--saldenliste", required=True)
    x = jn.add_parser("lexware", help="Lexware-Kontenübersicht des Jahres importieren (ersetzt den vorigen Import)")
    x.add_argument("--jahr", required=True, type=int)
    x.add_argument("--datei", required=True)
    with_json(jn.add_parser("buchen", help="Abschluss- oder Korrekturbuchung mit Begründung"))
    x = jn.add_parser("loeschen", help="Buchung von Hand zurücknehmen")
    x.add_argument("--jahr", required=True, type=int)
    x.add_argument("--ref", required=True)
    x.add_argument("--grund", required=True)
    x = jn.add_parser("konto", help="Kontenblatt")
    x.add_argument("konto")
    x.add_argument("--jahr", required=True, type=int)

    ex = area("export", "Export")
    x = ex.add_parser("journal")
    x.add_argument("jahr", type=int)
    x.add_argument("--out", required=True)
    x = ex.add_parser("saldenliste", help="Saldenliste CSV (E-Bilanz, Vortrag ins Folgejahr)")
    x.add_argument("jahr", type=int)
    x.add_argument("--out", required=True)

    pl = area("pipeline", "Angebote und Serien")
    pl.add_parser("pruefen", help="abgelaufene Angebote ablehnen, fällige Serienrechnungen anlegen, Nachfassen listen")
    se = area("serie", "Serienrechnungen")
    with_json(se.add_parser("anlegen"))
    se.add_parser("liste")

    ab = area("abo", "Abo-Übersicht")
    with_json(ab.add_parser("anlegen"))
    x = with_json(ab.add_parser("aendern"))
    x.add_argument("id", type=int)
    ab.add_parser("liste")

    el = area("elster", "E-Bilanz über ERiC (Spec 18.9); senden nur in der Oberfläche")
    x = el.add_parser("saldenliste", help="Saldenliste eines Jahres ablegen, das Kontor nicht selbst gebucht hat")
    x.add_argument("--jahr", type=int, required=True)
    x.add_argument("--datei", required=True)
    x = el.add_parser("pruefen", help="E-Bilanz erzeugen und lokal von ERiC prüfen lassen")
    x.add_argument("--jahr", type=int, required=True)
    x.add_argument("--out", help="Datensatz zusätzlich als XML speichern")
    el.add_parser("uebermittlungen", help="gesendete E-Bilanzen mit Transferticket")
    pr = area("profil", "Profilschalter des Mandanten (Spec 18.3)")
    pr.add_parser("zeigen")
    with_json(pr.add_parser("setzen"))
    nk = area("nummernkreis", "Nummernkreise")
    st = nk.add_parser("start", help="letzte vor dem Kontor vergebene Nummer (z. B. aus Lexware)")
    st.add_argument("--kreis", required=True, choices=["RE", "AN"])
    st.add_argument("--letzte", required=True, type=int)
    alt = area("altbestand", "offene Posten aus einem anderen Programm übernehmen (Spec 18.3)")
    lx = alt.add_parser("lexware", help="Rechnung aus der Lexware-API mit PDF und Zahlungen")
    lx.add_argument("--rechnung", required=True, help="JSON von /invoices/{id}")
    lx.add_argument("--zahlungen", required=True, help="JSON von /payments/{id}")
    lx.add_argument("--pdf", required=True, help="Lexware-PDF der Rechnung")
    lx.add_argument("--ohne-zahlungen", action="store_true", help="Zahlungen kommen aus dem Bankimport")
    lb = alt.add_parser("lexware-beleg", help="Eingangsbeleg aus der Lexware-API (/vouchers/{id}) mit Datei")
    lb.add_argument("--beleg", required=True, help="JSON von /vouchers/{id}")
    lb.add_argument("--datei", required=True)
    sub.add_parser("mandanten", help="Mandanten aus dem Register")
    sub.add_parser("pruefen", help="Protokoll, Archiv, Nummernkreise prüfen")
    sub.add_parser("backup", help="konsistente Kopie der Datenbank").add_argument("--out", required=True)
    sub.add_parser("server", help=f"Oberfläche auf http://{HOST}:{PORT}").add_argument("--port", type=int, default=PORT)
    return p


def run(a: argparse.Namespace, ctx) -> object:
    key = (a.area, getattr(a, "action", None))
    data = _load(getattr(a, "json", None))
    if key == ("einstellungen", "zeigen"):
        return md.get_settings(ctx)
    if key == ("einstellungen", "setzen"):
        md.set_settings(ctx, data)
        return md.get_settings(ctx)
    if key == ("einstellungen", "logo"):
        md.set_settings(ctx, {"logo_path": md.set_logo(ctx, Path(a.datei).read_bytes(), Path(a.datei).name)})
        return {"logo_path": md.get_settings(ctx)["logo_path"]}
    if a.area == "einrichtung":
        return md.onboarding_status(ctx)
    if key == ("kunde", "anlegen"):
        return md.get_customer(ctx, md.create_customer(ctx, data))
    if key == ("kunde", "liste"):
        return md.list_customers(ctx)
    if key == ("kunde", "zeigen"):
        return md.get_customer(ctx, a.id)
    if key == ("kunde", "aendern"):
        md.update_customer(ctx, a.id, data)
        return md.get_customer(ctx, a.id)
    if key == ("position", "anlegen"):
        return md.get_catalog_item(ctx, md.create_catalog_item(ctx, data))
    if key == ("position", "liste"):
        return md.list_catalog(ctx, a.suche)
    if key == ("textvorlage", "anlegen"):
        return {"id": md.create_template(ctx, data)}
    if key == ("textvorlage", "liste"):
        return md.list_templates(ctx)
    if key in (("angebot", "entwurf"), ("rechnung", "entwurf")):
        if not a.id:
            data.setdefault("kind", a.area)
        return _doc(ctx, docs.save_draft(ctx, data, a.id))
    if key == ("rechnung", "aus-angebot"):
        mode = "schluss" if a.schluss else ("abschlag" if a.abschlag else "voll")
        return _doc(ctx, docs.invoice_from_offer(ctx, a.id, mode, a.abschlag))
    if key == ("dokument", "liste"):
        return [{k: v for k, v in d.items() if k != "snapshot_json"}
                for d in docs.list_documents(ctx, a.gruppe, a.status, a.jahr)]
    if key == ("dokument", "zeigen"):
        return _doc(ctx, a.id)
    if key == ("dokument", "vorschau"):
        Path(a.out).write_bytes(fin.preview_pdf(ctx, a.id))
        return {"pdf": str(Path(a.out).resolve())}
    if key == ("dokument", "loeschen"):
        docs.delete_draft(ctx, a.id)
        return {"geloescht": a.id}
    if key == ("dokument", "status"):
        docs.set_status(ctx, a.id, a.neu)
        return _doc(ctx, a.id)
    if key == ("dokument", "als-position"):
        return md.get_catalog_item(ctx, docs.save_line_as_catalog(ctx, a.id, a.position))
    if key == ("zahlung", "erfassen"):
        return {"id": pay.record_payment(ctx, int(data["document_id"]), data["pay_date"], data["amount"],
                                         data.get("note", ""))}
    if key == ("zahlung", "ust"):
        return {"id": pay.record_tax_payment(ctx, data["pay_date"], data["amount"], data.get("note", ""))}
    if key == ("zahlung", "liste"):
        return pay.list_payments(ctx, year=a.jahr)
    if key == ("zahlung", "ohne-rechnung"):
        return {"id": pay.record_unbilled(ctx, data["pay_date"], data["amount"], str(data["tax_rate"]),
                                          data.get("note", ""))}
    if key == ("zahlung", "verknuepfen"):
        pay.link_unbilled(ctx, a.id, a.dokument)
        return _doc(ctx, a.dokument)
    if key == ("beleg", "erfassen"):
        path = Path(a.datei)
        return rc.add_file(ctx, path.read_bytes(), path.name, "cli")
    if key == ("beleg", "vorschlag"):
        rc.update(ctx, a.id, data)
        return rc.get_receipt(ctx, a.id)
    if key == ("beleg", "liste"):
        return rc.list_receipts(ctx, a.status)
    if key == ("beleg", "eigenbeleg"):
        return rc.create_own_receipt(ctx, data)
    if key == ("beleg", "zeigen"):
        return rc.get_receipt(ctx, a.id)
    if key == ("profil", "zeigen"):
        return md.get_profile(ctx)
    if key == ("profil", "setzen"):
        md.set_profile(ctx, data)
        return md.get_profile(ctx)
    if key == ("nummernkreis", "start"):
        md.set_counter_start(ctx, a.kreis, a.letzte)
        return {"kreis": a.kreis, "letzte": a.letzte}
    if key == ("altbestand", "lexware-beleg"):
        from ..importers import lexware
        rid = lexware.import_purchase_voucher(ctx, json.loads(Path(a.beleg).read_text(encoding="utf-8")),
                                              Path(a.datei).read_bytes(), Path(a.datei).name)
        return rc.get_receipt(ctx, rid)
    if key == ("altbestand", "lexware"):
        from ..importers import lexware
        did = lexware.import_invoice(ctx, json.loads(Path(a.rechnung).read_text(encoding="utf-8")),
                                     json.loads(Path(a.zahlungen).read_text(encoding="utf-8")),
                                     Path(a.pdf).read_bytes(), with_payments=not a.ohne_zahlungen)
        return _doc(ctx, did)
    if key == ("eingang", "abholen"):
        if not md.has(ctx, "telegram_eingang"):
            raise KontorError("Dieser Mandant hat keinen Telegram-Eingang (Profilschalter telegram_eingang).")
        from ..importers.telegram import SshTransport, pull
        return pull(ctx, SshTransport(a.host))
    if key == ("pipeline", "pruefen"):
        from ..services import pipeline, series
        return {"abgelaufen": pipeline.expire_offers(ctx), "serienentwuerfe": series.generate_due(ctx),
                "nachfassen": pipeline.open_followups(ctx)}
    if key == ("serie", "anlegen"):
        from ..services import series
        return series.get(ctx, series.create(ctx, data))
    if key == ("serie", "liste"):
        from ..services import series
        return series.list_all(ctx)
    if a.area == "abo":
        from ..services import subscriptions as subs
        if a.action == "anlegen":
            return subs.get(ctx, subs.create(ctx, data))
        if a.action == "aendern":
            subs.update(ctx, a.id, data)
            return subs.get(ctx, a.id)
        return subs.overview(ctx)
    if key == ("auswertung", "bilanz"):
        from ..reports.abschluss import bilanz_guv
        return bilanz_guv(ctx, a.jahr, a.stichtag)
    if key == ("auswertung", "auffaelligkeiten"):
        from ..reports.abschluss import auffaelligkeiten
        return auffaelligkeiten(ctx, a.jahr)
    if a.area == "bank":
        from ..services import bank
        if a.action == "konto":
            bank.add_account(ctx, a.iban, a.name, a.konto)
            return bank.accounts(ctx)
        if a.action == "konten":
            return list(bank.accounts(ctx).values())
        if a.action == "import":
            return bank.import_datev(ctx, Path(a.datei).read_text(encoding="latin-1"))
        if a.action == "liste":
            return [{k: v for k, v in t.items() if k != "raw_json"} for t in bank.transactions(ctx, a.jahr, a.offen)]
        if a.action == "zuordnen":
            ids = []
            for num in a.nummer or []:
                row = ctx.conn.execute("SELECT id FROM documents WHERE number = ?", (num,)).fetchone()
                if not row:
                    raise KontorError(f"Rechnung {num} gibt es nicht.")
                ids.append(row[0])
            bank.assign(ctx, a.id, a.art, document_ids=ids or None, account=a.konto, tax_key=a.steuer,
                        receipt_id=a.beleg, note=a.notiz)
            return {"zugeordnet": a.id}
        if a.action == "loesen":
            bank.unassign(ctx, a.id, a.grund)
            return {"geloest": a.id}
        if a.action == "dublette-echt":
            bank.confirm_duplicate(ctx, a.id, a.grund)
            return {"bestaetigt": a.id}
        if a.action == "regel":
            bank.add_rule(ctx, a.muster, a.konto, a.steuer)
            return {"regel": a.muster.upper()}
        if a.action == "regeln-lernen":
            return {"regeln": bank.learn_rules(ctx, Path(a.datei).read_text(encoding="utf-8"))}
        if a.action == "vorschlaege":
            n = bank.apply_suggestions(ctx, a.jahr)
            return {"zugeordnet": n, "belege_verknuepft": bank.link_receipts(ctx, a.jahr),
                    "offen": len(bank.transactions(ctx, a.jahr, offen=True))}
        if a.action == "anker":
            bank.set_anchor(ctx, a.iban, a.datum, a.saldo, a.notiz)
            return bank.anchors(ctx)
    if a.area == "btc":
        return _btc(a, ctx, data)
    if a.area == "lohn":
        return _lohn(a, ctx, data)
    if a.area == "auftrag":
        return _auftrag(a, ctx)
    if a.area == "journal":
        from ..services import journal as jr
        if a.action == "eroeffnung":
            return {"konten": jr.opening_from_saldenliste(ctx, a.jahr, Path(a.saldenliste).read_text(encoding="utf-8"))}
        if a.action == "lexware":
            from ..reports.abschluss import auffaelligkeiten
            n = jr.import_lexware(ctx, a.jahr, Path(a.datei).read_text(encoding="utf-8"))
            return {"zeilen": n, "auffaelligkeiten": [f["text"] for f in auffaelligkeiten(ctx, a.jahr)]}
        if a.action == "buchen":
            return {"ref": jr.book(ctx, data)}
        if a.action == "loeschen":
            jr.delete_manual(ctx, a.jahr, a.ref, a.grund)
            return {"geloescht": a.ref}
        return jr.lines(ctx, a.jahr, a.konto)
    if a.area == "elster":
        from ..services import elster
        if a.action == "saldenliste":
            return elster.saldenliste_ablegen(ctx, a.jahr, Path(a.datei).read_text(encoding="utf-8"))
        if a.action == "pruefen":
            if a.out:
                Path(a.out).write_text(elster.datensatz(ctx, a.jahr, test=True, hersteller_id=md.get_settings(ctx)[
                    "elster_hersteller_id"] or elster.PLATZHALTER_HERSTELLER_ID), encoding="utf-8")
            return elster.pruefen(ctx, a.jahr)
        return elster.uebermittlungen(ctx)
    if key == ("export", "saldenliste"):
        from ..reports.abschluss import saldenliste_csv
        Path(a.out).write_text(saldenliste_csv(ctx, a.jahr), encoding="utf-8")
        return {"csv": str(Path(a.out).resolve())}
    if key == ("auswertung", "ustva"):
        return ustva(ctx, a.zeitraum)
    if key == ("auswertung", "euer"):
        return euer(ctx, a.jahr)
    if key == ("auswertung", "uebersicht"):
        o = overview(ctx)
        for d in o["open_invoices"] + o["drafts"]:
            d.pop("snapshot_json", None)
        return o
    if key == ("export", "journal"):
        Path(a.out).write_text(journal_csv(ctx, a.jahr), encoding="utf-8")
        return {"csv": str(Path(a.out).resolve())}
    if a.area == "pruefen":
        problems = integrity.check(ctx)
        return {"ok": not problems, "probleme": problems}
    if a.area == "backup":
        dst = sqlite3.connect(a.out)
        ctx.conn.backup(dst)
        dst.close()
        return {"backup": str(Path(a.out).resolve())}
    raise KontorError(f"Unbekannter Befehl: {' '.join(filter(None, key))}")


def _auftrag(a: argparse.Namespace, ctx) -> object:
    from ..services import auftraege
    if a.action == "liste":
        return auftraege.liste(ctx, a.status)
    if a.action == "verwerfen":
        auftraege.verwerfen(ctx, a.id, a.grund)
        return auftraege.get(ctx, a.id)
    art = {"abholen": "elstam_abholen"}.get(a.action, a.action)
    p = {k: getattr(a, k) for k in ("jahr", "zeitraum", "mitarbeiter", "vorgang", "referenzdatum") if hasattr(a, k)}
    return auftraege.anlegen(ctx, art, p, a.test, a.notiz)


def _lohn(a: argparse.Namespace, ctx, data: dict) -> object:
    from ..services import lohn
    if a.action == "mitarbeiter":
        if data:
            return lohn.get(ctx, lohn.mitarbeiter_speichern(ctx, data, a.id))
        return lohn.mitarbeiter(ctx)
    if a.action == "zeigen":
        return lohn.get(ctx, a.id)
    if a.action == "merkmale":
        return lohn.merkmale_setzen(ctx, a.id, a.ab, data)
    if a.action == "bezug":
        return {"bezug": lohn.bezug(ctx, a.id, data)}
    if a.action == "bezug-beenden":
        lohn.bezug_beenden(ctx, a.bezug, a.bis)
        return {"bezug": a.bezug, "bis": a.bis}
    if a.action == "vortrag":
        lohn.vortrag_setzen(ctx, a.id, a.jahr, a.bis, data, a.notiz)
        return lohn.get(ctx, a.id)["vortrag"]
    if a.action == "abrechnen":
        return lohn.berechnen(ctx, a.id, a.monat) if a.id else lohn.abrechnungen(ctx, a.monat)
    if a.action == "journal":
        return lohn.lohnjournal(ctx, a.jahr, a.monat)
    if a.action == "lohnkonto":
        return lohn.lohnkonto(ctx, a.id, a.jahr)
    if a.action == "kosten":
        return lohn.lohnkosten(ctx, a.jahr)
    if a.action == "kassen":
        return lohn.beitraege_kassen(ctx, a.monat)
    if a.action == "lsta":
        if a.pruefen:
            return lohn.lsta_pruefen(ctx, a.jahr, a.zeitraum or "")
        return lohn.lsta_werte(ctx, a.jahr, a.zeitraum) if a.zeitraum else lohn.zeitraeume(ctx, a.jahr)
    if a.action == "lstb":
        return lohn.lstb_pruefen(ctx, a.id, a.jahr) if a.pruefen else lohn.lstb_werte(ctx, a.id, a.jahr)
    if a.action == "auffaelligkeiten":
        return lohn.auffaelligkeiten(ctx, a.jahr)
    if a.action == "festschreiben":
        ids = lohn.festschreiben_bis(ctx, a.id, a.monat) if a.bis else [lohn.festschreiben(ctx, a.id, a.monat)]
        return [{k: x[k] for k in ("id", "monat", "name", "summen", "pdf")} for x in map(lambda i: lohn.abrechnung(ctx, i), ids)]
    if a.action == "stornieren":
        lohn.stornieren(ctx, a.abrechnung, a.grund)
        return lohn.abrechnung(ctx, a.abrechnung)
    if a.action == "lsta-extern":
        return lohn.lsta_extern(ctx, a.jahr, a.zeitraum, a.notiz)
    if a.action == "elstam":
        if a.uebernehmen:
            return lohn.elstam_uebernehmen(ctx, a.uebernehmen)
        if a.pruefen:
            return lohn.elstam_pruefen(ctx, a.pruefen, [a.id], a.referenzdatum)
        return lohn.elstam(ctx, a.offen)
    raise KontorError(f"Unbekannter Befehl: lohn {a.action}")


def _btc(a: argparse.Namespace, ctx, data: dict) -> object:
    from ..services import bitcoin as btc
    if not md.has(ctx, "bitcoin"):
        raise KontorError("Bitcoin ist für diesen Mandanten aus (Profilschalter bitcoin, `kontor profil setzen`).")
    if a.action == "wallet":
        if a.name:
            btc.wallet(ctx, a.name, a.art, not a.inaktiv)
        return btc.wallets(ctx)
    if a.action == "import":
        return btc.importieren(ctx, a.format, Path(a.datei).read_text(encoding="utf-8-sig"), a.wallet)
    if a.action == "erfassen":
        return btc.get(ctx, btc.erfassen(ctx, data))
    if a.action == "liste":
        out = btc.bewegungen(ctx, a.jahr, a.status)
        for m in out:
            m["vorschlag"] = btc.vorschlag(ctx, m)
            m["bank_vorschlag"] = btc.bank_vorschlag(ctx, m) if m["art"] in ("kauf", "verkauf") else None
        return out
    if a.action == "bestaetigen":
        if a.zurueck:
            btc.entbestaetigen(ctx, a.id, a.grund)
        else:
            btc.bestaetigen(ctx, a.id)
        return btc.get(ctx, a.id)
    if a.action == "zuordnen":
        if a.verwerfen:
            btc.verwerfen(ctx, a.id, a.grund)
        elif a.bank:
            return {"bewegung": a.id, "bankumsatz": btc.bank_zuordnen(ctx, a.id)}
        elif a.vorschlag:
            s = btc.vorschlag(ctx, btc.get(ctx, a.id))
            if not s:
                raise KontorError(f"Für Bewegung {a.id} gibt es keinen Vorschlag.")
            btc.zuordnen(ctx, a.id, art=s["art"], rechnung=s["nummer"], eur=s["eur"])
        else:
            btc.zuordnen(ctx, a.id, art=a.art, rechnung=a.rechnung, beleg=a.beleg, konto=a.konto, steuer=a.steuer,
                         ziel=a.ziel, eur=a.eur,
                         gebuehr_eur=a.gebuehr_eur, gebuehr_sats=a.gebuehr_sats, vertragsdatum=a.vertragsdatum,
                         ak=a.ak, notiz=a.notiz)
        return btc.get(ctx, a.id)
    if a.action == "saldo":
        if a.wallet:
            if not a.datum or a.sats is None:
                raise KontorError("--datum und --sats angeben (Saldo, den die Wallet zeigt).")
            btc.saldo_eintragen(ctx, a.wallet, a.datum, a.sats, a.notiz)
        return btc.salden(ctx)
    if a.action == "kurse":
        if a.abrufen:
            import urllib.request

            from datetime import datetime, timezone

            from ..importers.bitcoin import BITSTAMP_URL, KRAKEN_URL
            # Die Abfragen nennen nur das Handelspaar, nichts über den Bestand. Kraken zuerst (maßgeblich), dann
            # Bitstamp für die Historie davor und für Tage, die Kraken nicht liefert; fällt eine Quelle aus, läuft
            # die andere trotzdem.
            def holen(url: str) -> str:
                req = urllib.request.Request(url, headers={"User-Agent": "kontor"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    return resp.read().decode("utf-8")
            neu, fehler = {"kraken": 0, "bitstamp": 0}, []
            try:
                neu["kraken"] = btc.abruf_speichern(ctx, "kraken", holen(KRAKEN_URL + str(btc.kraken_seit(ctx))))
            except (OSError, KontorError) as exc:
                fehler.append(f"Kraken: {exc}")
            start, jetzt = btc.bitstamp_ab(ctx, a.ab), int(datetime.now(timezone.utc).timestamp())
            try:
                while start < jetzt - 86400:              # höchstens 1000 Tage je Abfrage
                    neu["bitstamp"] += btc.abruf_speichern(ctx, "bitstamp", holen(BITSTAMP_URL + str(start)))
                    start += 1000 * 86400
            except (OSError, KontorError) as exc:
                fehler.append(f"Bitstamp: {exc}")
            if fehler and not any(neu.values()):
                raise KontorError("Keine Kurse abgerufen: " + "; ".join(fehler))
            return {"neu": neu, "fehler": fehler, **btc.kurse_stand(ctx)}
        if a.datum or a.eur:
            if not (a.datum and a.eur):
                raise KontorError("--datum und --eur zusammen angeben.")
            btc.kurs_speichern(ctx, a.datum, a.eur)
        return btc.kurse_stand(ctx)
    if a.action == "bewertung":
        if a.vortrag:
            if a.sats is None or a.ak is None or a.buchwert is None:
                raise KontorError("Vortrag: --sats, --ak und --buchwert angeben.")
            btc.vortrag_setzen(ctx, a.jahr, a.vortrag, a.sats, a.ak, a.buchwert)
        return btc.bewertung(ctx, a.jahr)
    raise KontorError(f"Unbekannter Befehl: btc {a.action}")


def init(a: argparse.Namespace) -> dict:
    """Datenordner anlegen (Datenbank, archiv/, belege/, eingang/) und, mit --id, im Register eintragen.
    Ändert nie einen bestehenden Registereintrag; ein vorhandener Datenordner wird nur geöffnet."""
    terms = config.terms_accepted()
    if not terms:
        if not a.bedingungen_gelesen:
            raise ConfigError("Bitte zuerst TERMS.md und DISCLAIMER.md lesen: Kontor ist nur für die eigene Buchhaltung, "
                              "keine Steuerberatung, ohne Gewähr; für andere zu buchen braucht die gesetzliche Befugnis. "
                              "Wenn du einverstanden bist: dieselbe Zeile mit --bedingungen-gelesen.")
        terms = config.accept_terms()
    data = _checked(Path(a.daten))
    register = None
    if a.id:
        if not ID_PATTERN.match(a.id):
            raise ConfigError(f"Mandanten-ID {a.id!r}: nur Kleinbuchstaben, Ziffern und Bindestrich.")
        register = register_path()
        raw = json.loads(register.read_text(encoding="utf-8")) if register.is_file() else {"mandanten": []}
        for entry in raw.get("mandanten", []):
            if entry.get("id") == a.id:
                raise ConfigError(f"Mandant {a.id!r} steht schon im Register {register}.")
        raw.setdefault("mandanten", []).append({"id": a.id, "name": a.name or a.id, "daten": str(data),
                                                "farbe": a.farbe})
        register.parent.mkdir(parents=True, exist_ok=True)
        register.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    ctx = open_ctx("cli", data)
    try:
        status = md.onboarding_status(ctx)
    finally:
        ctx.conn.close()
    return {"daten": str(data), "register": str(register) if register else None, "mandant": a.id,
            "bedingungen": terms, "einrichtung": status}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.area == "server":
        import uvicorn

        from ..api.app import create_app
        uvicorn.run(create_app(), host=HOST, port=args.port)
        return 0
    if args.area == "init":
        try:
            result = init(args)
        except (ConfigError, KontorError, OSError, json.JSONDecodeError) as exc:
            print(json.dumps({"fehler": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0
    if args.area == "mandanten":
        try:
            entries = [{"id": m.id, "name": m.name, "daten": str(m.data)} for m in mandanten()]
        except ConfigError as exc:
            print(json.dumps({"fehler": str(exc)}, ensure_ascii=False), file=sys.stderr)
            return 1
        print(json.dumps({"eintraege": entries}, ensure_ascii=False, indent=2))
        return 0
    try:
        m = mandant(args.mandant)
        ctx = open_ctx("cli", m.data)
    except ConfigError as exc:
        print(json.dumps({"fehler": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    try:
        result = run(args, ctx)
    except (KontorError, ValueError, KeyError, json.JSONDecodeError, sqlite3.IntegrityError) as exc:
        print(json.dumps({"fehler": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    finally:
        ctx.conn.close()
    # Jede Ausgabe nennt den Mandanten (Spec 18.2); Listen stehen unter "eintraege".
    out = {"mandant": m.id, **result} if isinstance(result, dict) else {"mandant": m.id, "eintraege": result}
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    return 1 if args.area == "pruefen" and not result["ok"] else 0


if __name__ == "__main__":
    sys.exit(main())
