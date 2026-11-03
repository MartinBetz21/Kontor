"""Umsatzsteuer-Voranmeldung über ERiC (Profilschalter `ustva_eric`). Die Werte kommen aus `reports.ustva`, also aus
denselben Zahlen wie die Umsatzsteuer-Seite. Gesendet wird nur als Auftrag zur Freigabe durch den Menschen (Spec
„Aufträge“); jede Übermittlung steht im Protokoll (`elster_senden`, Objekt `ustva`), Datensatz und ELSTER-Protokoll gehen
schreibgeschützt nach `archiv/elster/`.

Kontor legt keine Voranmeldung an, solange im Zeitraum Belege unbestätigt sind: deren Vorsteuer fehlte sonst still."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path

from ..context import Ctx, require_human
from ..core.errors import KontorError
from ..core.tax_codes import period_bounds
from ..elster import eric
from ..elster import ustva as uv
from ..elster.lohn import Arbeitgeber
from ..reports.ustva import ustva as ustva_werte
from ..store import audit
from ..store.db import tx
from . import masterdata as md

PLATZHALTER_HERSTELLER_ID = "00000"        # nur für die lokale Prüfung, bevor die eigene Hersteller-ID da ist
ERIC_NUR_HINWEISE = 610001003
MONATE = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November",
          "Dezember")
# Kontor-Feld → (Kennzahl, „basis“ in ganzen Euro oder „tax“ in Cent)
FELDER = (("81", "basis"), ("86", "basis"), ("21", "basis"), ("45", "basis"), ("46", "basis"), ("47", "tax"),
          ("84", "basis"), ("85", "tax"), ("66", "tax"), ("67", "tax"))


def name(spec: str) -> str:
    jahr, z = uv.zeitraum(spec)
    return f"{MONATE[int(z) - 1]} {jahr}" if int(z) <= 12 else f"{int(z) - 40}. Quartal {jahr}"


def _require(ctx: Ctx) -> None:
    if not md.has(ctx, "ustva_eric"):
        raise KontorError("Voranmeldung über ERiC ist für diesen Mandanten aus (Profilschalter ustva_eric).")
    if md.get_settings(ctx)["ustva_period"] == "jahr":
        raise KontorError("Dieser Mandant gibt keine Voranmeldungen ab (Voranmeldezeitraum: Jahr).")


def _objekt(spec: str) -> int:
    jahr, z = uv.zeitraum(spec)
    return jahr * 100 + int(z)


def uebermittlungen(ctx: Ctx, spec: str | None = None) -> list[dict]:
    """Gesendete Voranmeldungen (auch Tests und Ablehnungen), neueste zuerst."""
    sql, args = "SELECT at, object_id, payload FROM audit_log WHERE action = 'elster_senden' AND object = 'ustva'", []
    if spec:
        sql += " AND object_id = ?"
        args.append(_objekt(spec))
    return [{"am": r["at"], **json.loads(r["payload"])} for r in ctx.conn.execute(sql + " ORDER BY id DESC", args)]


def offen(ctx: Ctx, spec: str) -> list[str]:
    """Was im Zeitraum noch fehlt, bevor die Werte stimmen: unbestätigte Belege (mit Datum im Zeitraum oder ohne)."""
    a, b = (d.isoformat() for d in period_bounds(spec))
    return [f"Beleg {r['id']} ({r['supplier'] or r['original_name'] or 'ohne Lieferant'}"
            f"{', ' + r['receipt_date'] if r['receipt_date'] else ', ohne Datum'}) unbestätigt"
            for r in ctx.conn.execute("SELECT id, supplier, original_name, receipt_date FROM receipts "
                                      "WHERE status = 'eingang' AND (receipt_date IS NULL OR receipt_date BETWEEN ? AND ?) "
                                      "ORDER BY id", (a, b))]


def werte(ctx: Ctx, spec: str) -> dict:
    """Die Kennzahlen, die gesendet würden, und ob es eine Berichtigung ist."""
    _require(ctx)
    jahr, z = uv.zeitraum(spec)
    uv.datenart(jahr)
    if period_bounds(spec)[1] >= ctx.now_date():
        raise KontorError(f"{name(spec)} ist noch nicht vorbei.")
    fehlt = offen(ctx, spec)
    if fehlt:
        raise KontorError(f"Erst bestätigen, sonst fehlt die Steuer daraus: {'; '.join(fehlt)}.")
    u = ustva_werte(ctx, spec)
    kz = {n: u["fields"][n][teil] for n, teil in FELDER if u["fields"][n][teil]}
    a = uv.UStVA(jahr, z, kz)
    if a.kz83 != u["zahllast"]:
        raise KontorError(f"Zahllast passt nicht zu den Kennzahlen ({a.kz83} statt {u['zahllast']} Cent).")
    berichtigt = any(m["ok"] and not m["test"] for m in uebermittlungen(ctx, spec))
    return {"zeitraum": spec, "kz": kz, "kz83": a.kz83, "berichtigt": berichtigt, "faellig": u["deadline"]}


def _unternehmer(ctx: Ctx, steuernummer: bool = True) -> Arbeitgeber:
    s = md.get_settings(ctx)
    needed = (("name", "Name"), ("street", "Straße"), ("postcode", "PLZ"), ("city", "Ort"), ("tax_number", "Steuernummer"),
              ("finanzamt_nr", "Finanzamtsnummer"))
    missing = [label for key, label in needed if not s[key]]
    if missing:
        raise KontorError(f"Für die Voranmeldung fehlt in den Einstellungen: {', '.join(missing)}.")
    m = re.fullmatch(r"(.*?)\s+(\d+\s*[a-zA-Z]?(?:\s*-\s*\d+\s*[a-zA-Z]?)?)", s["street"].strip())
    strasse, hausnummer = (m.group(1), m.group(2)) if m else (s["street"], "")
    if s["eric_pfad"]:
        os.environ["KONTOR_ERIC"] = s["eric_pfad"]
    stnr = eric.steuernummer13(s["tax_number"], s["finanzamt_nr"]) if steuernummer else ""
    return Arbeitgeber(s["name"], strasse, hausnummer, s["postcode"], s["city"], stnr, telefon=s["phone"],
                       email=s["email"])


def _datensatz(ctx: Ctx, spec: str, w: dict, test: bool, hersteller_id: str) -> str:
    s = md.get_settings(ctx)
    jahr, z = uv.zeitraum(spec)
    widnr = (s["vat_id"] or "").replace(" ", "").upper()
    a = uv.UStVA(jahr, z, w["kz"], w["berichtigt"], ctx.now_date(), widnr if re.fullmatch(r"DE\d{9}", widnr) else "")
    return uv.ustva(a, _unternehmer(ctx), hersteller_id, test, s["managing_directors"] or s["name"])


def pruefen(ctx: Ctx, spec: str) -> dict:
    """Lokale Prüfung durch ERiC, ohne Versand. Nur Hinweise (Code 610001003) gelten als plausibel."""
    w = werte(ctx, spec)
    hid = md.get_settings(ctx)["elster_hersteller_id"] or PLATZHALTER_HERSTELLER_ID
    r = eric.pruefen(_datensatz(ctx, spec, w, True, hid), uv.datenart(uv.zeitraum(spec)[0]))
    if r["code"] == ERIC_NUR_HINWEISE:
        r = {**r, "ok": True, "text": "Plausibel, mit Hinweisen."}
    return {"titel": f"Umsatzsteuer-Voranmeldung {name(spec)}", "werte": w,
            **{k: r[k] for k in ("ok", "code", "text", "meldungen")}}


def senden(ctx: Ctx, spec: str, pin: str, test: bool) -> dict:
    """Voranmeldung an ELSTER (nur der Mensch, über „Freigaben“)."""
    require_human(ctx)
    s = md.get_settings(ctx)
    if not s["elster_hersteller_id"]:
        raise KontorError("Senden braucht die eigene Hersteller-ID (Einstellungen → ELSTER).")
    if not s["elster_zertifikat"]:
        raise KontorError("Pfad zum ELSTER-Zertifikat (.pfx) fehlt (Einstellungen → ELSTER).")
    if not pin:
        raise KontorError("PIN des Zertifikats eingeben.")
    w = werte(ctx, spec)
    xml = _datensatz(ctx, spec, w, test, s["elster_hersteller_id"])
    folder = ctx.data / "archiv" / "elster"
    folder.mkdir(parents=True, exist_ok=True)
    base = folder / f"ustva-{spec}-{'test' if test else 'echt'}-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    r = eric.senden(xml, uv.datenart(uv.zeitraum(spec)[0]), Path(s["elster_zertifikat"]), pin, base.with_suffix(".pdf"))
    if r["code"] == ERIC_NUR_HINWEISE:
        r = {**r, "ok": True}
    base.with_suffix(".xml").write_text(xml, encoding="utf-8")
    for f in (base.with_suffix(".xml"), base.with_suffix(".pdf")):
        if f.is_file():
            os.chmod(f, 0o444)
    with tx(ctx.conn):
        audit.log(ctx.conn, ctx.actor, "elster_senden", "ustva", _objekt(spec),
                  {"zeitraum": spec, "test": test, "ok": r["ok"], "code": r["code"], "transferticket": r["transferticket"],
                   "kz": w["kz"], "kz83": w["kz83"], "berichtigt": w["berichtigt"],
                   "datensatz": base.with_suffix(".xml").name,
                   "protokoll": Path(r["protokoll"]).name if r["protokoll"] else ""})
    return {"titel": f"Umsatzsteuer-Voranmeldung {name(spec)}", "test": test,
            **{k: r[k] for k in ("ok", "code", "text", "meldungen", "transferticket", "protokoll")}}
