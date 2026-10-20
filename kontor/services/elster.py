"""E-Bilanz aus den Daten eines Mandanten erzeugen, prüfen und senden (Spec 18.9). Salden kommen aus dem Journal,
für ältere Jahre aus einer Saldenliste im Datenordner (`abgaben/saldenliste-<jahr>.csv`). Senden ist eine menschliche
Handlung: nur Akteur `ui`, PIN wird nie gespeichert, Datensatz und Protokoll gehen schreibgeschützt ins Archiv."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from ..context import Ctx, require_human
from ..core.errors import KontorError
from ..elster import ebilanz as eb
from ..elster import eric
from ..importers.lexware import parse_saldenliste
from ..reports.abschluss import saldenliste_csv
from ..store import audit
from ..store.db import tx
from . import masterdata as md

DATENART = f"Bilanz_{eb.TAXONOMIE}"
PLATZHALTER_HERSTELLER_ID = "00000"        # nur für die lokale Prüfung, bevor die eigene Hersteller-ID da ist


def _eric_dir(ctx: Ctx) -> None:
    path = md.get_settings(ctx)["eric_pfad"]
    if path:
        os.environ["KONTOR_ERIC"] = path


def _saldenliste_datei(ctx: Ctx, year: int) -> Path:
    return ctx.data / "abgaben" / f"saldenliste-{year}.csv"


def saldenliste_ablegen(ctx: Ctx, year: int, text: str) -> dict:
    """Saldenliste eines Jahres, das Kontor nicht selbst gebucht hat, für die E-Bilanz ablegen."""
    bal, _ = parse_saldenliste(text)
    if not bal:
        raise KontorError("Die Saldenliste ist leer oder hat nicht das Format Konto;Bezeichnung;Saldo;S/H.")
    target = _saldenliste_datei(ctx, year)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    with tx(ctx.conn):
        audit.log(ctx.conn, ctx.actor, "saldenliste_ablegen", "abgabe", year, {"konten": len(bal)})
    return {"jahr": year, "konten": len(bal), "datei": str(target)}


def jahre(ctx: Ctx) -> list[int]:
    folder = ctx.data / "abgaben"
    abgelegt = {int(m.group(1)) for f in folder.glob("saldenliste-*.csv") if (m := re.search(r"(\d{4})", f.name))} \
        if folder.is_dir() else set()
    gebucht = {int(r[0]) for r in ctx.conn.execute("SELECT DISTINCT year FROM journal")}
    return sorted(abgelegt | gebucht, reverse=True)


def bericht(ctx: Ctx, year: int) -> eb.Bericht:
    f = _saldenliste_datei(ctx, year)
    text = f.read_text(encoding="utf-8") if f.is_file() else saldenliste_csv(ctx, year)
    bal, names = parse_saldenliste(text)
    if not bal:
        raise KontorError(f"Für {year} gibt es weder Buchungen noch eine abgelegte Saldenliste.")
    return eb.Bericht(year, bal, names, ctx.now_date())


def stammdaten(ctx: Ctx, steuernummer: bool = True) -> eb.Stammdaten:
    """Ohne `steuernummer` bleibt die 13-stellige Nummer leer: genug für die Kennzahlen, ERiC wird nicht gebraucht."""
    s = md.get_settings(ctx)
    needed = (("name", "Name"), ("street", "Straße"), ("postcode", "PLZ"), ("city", "Ort"), ("share_capital", "Stammkapital"))
    if steuernummer:
        needed += (("tax_number", "Steuernummer"), ("finanzamt_nr", "Finanzamtsnummer"))
    missing = [label for key, label in needed if not s[key]]
    if missing:
        raise KontorError(f"Für die E-Bilanz fehlt in den Einstellungen: {', '.join(missing)}.")
    m = re.fullmatch(r"(.*?)\s+(\d+\s*[a-zA-Z]?(?:\s*-\s*\d+\s*[a-zA-Z]?)?)", s["street"].strip())
    street, house = (m.group(1), m.group(2)) if m else (s["street"], "")
    _eric_dir(ctx)
    return eb.Stammdaten(s["name"], street, house, s["postcode"], s["city"],
                         eric.steuernummer13(s["tax_number"], s["finanzamt_nr"]) if steuernummer else "",
                         md.to_cents(s["share_capital"]), md.to_cents(s["share_capital_outstanding"] or 0))


def datensatz(ctx: Ctx, year: int, test: bool, hersteller_id: str | None = None) -> str:
    s = md.get_settings(ctx)
    hid = hersteller_id or s["elster_hersteller_id"]
    if not hid:
        raise KontorError("Hersteller-ID fehlt (Einstellungen → ELSTER).")
    return eb.datensatz(bericht(ctx, year), stammdaten(ctx), hid, test, s["managing_directors"] or s["name"])


def kennzahlen(ctx: Ctx, year: int) -> dict:
    total, nachweis = eb.werte(bericht(ctx, year), stammdaten(ctx, steuernummer=False))
    return {"jahr": year, "bilanzsumme": total.get("bs.ass", 0), "eigenkapital": total.get("bs.eqLiab.equity", 0),
            "jahresergebnis": total.get("ismi.netIncome", 0), "umsatz": total.get("ismi.netIncome.netSales", 0),
            "kontennachweis": len(nachweis) if year >= 2025 else 0}


def pruefen(ctx: Ctx, year: int) -> dict:
    """Lokale Prüfung durch ERiC, ohne Versand. Ohne eigene Hersteller-ID mit Platzhalter."""
    xml = datensatz(ctx, year, test=True,
                    hersteller_id=md.get_settings(ctx)["elster_hersteller_id"] or PLATZHALTER_HERSTELLER_ID)
    result = eric.pruefen(xml, DATENART)
    return {"jahr": year, "datenart": DATENART, **{k: result[k] for k in ("ok", "code", "text", "meldungen")},
            **kennzahlen(ctx, year)}


def senden(ctx: Ctx, year: int, pin: str, test: bool) -> dict:
    require_human(ctx)
    if year >= ctx.now_date().year:
        raise KontorError(f"{year} läuft noch: Die E-Bilanz geht erst nach dem Abschluss des Jahres raus.")
    s = md.get_settings(ctx)
    if not s["elster_hersteller_id"]:
        raise KontorError("Senden braucht die eigene Hersteller-ID (Einstellungen → ELSTER).")
    if not s["elster_zertifikat"]:
        raise KontorError("Pfad zum ELSTER-Zertifikat (.pfx) fehlt (Einstellungen → ELSTER).")
    if not pin:
        raise KontorError("PIN des Zertifikats eingeben.")
    xml = datensatz(ctx, year, test)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = ctx.data / "archiv" / "elster"
    folder.mkdir(parents=True, exist_ok=True)
    base = folder / f"ebilanz-{year}-{'test' if test else 'echt'}-{stamp}"
    result = eric.senden(xml, DATENART, Path(s["elster_zertifikat"]), pin, base.with_suffix(".pdf"))
    base.with_suffix(".xml").write_text(xml, encoding="utf-8")
    for f in (base.with_suffix(".xml"), base.with_suffix(".pdf")):
        if f.is_file():
            os.chmod(f, 0o444)
    with tx(ctx.conn):
        audit.log(ctx.conn, ctx.actor, "elster_senden", "ebilanz", year,
                  {"test": test, "ok": result["ok"], "code": result["code"], "transferticket": result["transferticket"],
                   "datensatz": base.with_suffix(".xml").name, "protokoll": Path(result["protokoll"]).name
                   if result["protokoll"] else ""})
    return {"jahr": year, "test": test, **{k: result[k] for k in ("ok", "code", "text", "meldungen", "transferticket",
                                                                 "protokoll")}}


def uebermittlungen(ctx: Ctx) -> list[dict]:
    return [{"am": r["at"], **json.loads(r["payload"]), "jahr": r["object_id"]} for r in ctx.conn.execute(
        "SELECT at, object_id, payload FROM audit_log WHERE action = 'elster_senden' ORDER BY id DESC")]
