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
