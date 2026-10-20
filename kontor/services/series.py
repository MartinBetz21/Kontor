"""Serienrechnungen (z. B. Einführung über drei Monate, danach Betrieb monatlich). Jede fällige Periode
wird ein Rechnungsentwurf mit Leistungszeitraum; abschließen und verschicken bleibt beim Menschen."""
from __future__ import annotations

import calendar
import json
from datetime import date, timedelta

from ..context import Ctx
from ..core.errors import KontorError
from ..store import audit
from ..store.db import tx
from . import documents as docs
from . import masterdata as md
from .documents import _clean_line, _iso

MONTHS = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober",
          "November", "Dezember")


def add_months(day: date, months: int) -> date:
    m = day.month - 1 + months
    y, m = day.year + m // 12, m % 12 + 1
    return date(y, m, min(day.day, calendar.monthrange(y, m)[1]))


def create(ctx: Ctx, data: dict) -> int:
    customer = md.get_customer(ctx, int(data.get("customer_id") or 0))
    subject = str(data.get("subject") or "").strip()
    if not subject:
        raise KontorError("Serie braucht einen Betreff, z. B. „Einführung“.")
    lines = [_clean_line(l, i + 1) for i, l in enumerate(data.get("lines") or [])]
    if not lines:
        raise KontorError("Serie braucht mindestens eine Position.")
    start = _iso(data.get("start_date"), "Start")
    if not start:
        raise KontorError("Start der Serie fehlt (erster Leistungstag, z. B. Go-live).")
    count = int(data["count"]) if str(data.get("count") or "").strip() else None
    if count is not None and count < 1:
        raise KontorError("Anzahl muss mindestens 1 sein (leer = unbegrenzt).")
    interval = int(data.get("interval_months") or 1)
    with tx(ctx.conn):
        cur = ctx.conn.execute(
            "INSERT INTO recurring_invoices (customer_id, subject, lines_json, interval_months, start_date, count, "
            "next_date, source_document_id) VALUES (?,?,?,?,?,?,?,?)",
            (customer["id"], subject, json.dumps(lines, ensure_ascii=False), interval, start, count, start,
             data.get("source_document_id")))
        audit.log(ctx.conn, ctx.actor, "serie_anlegen", "serie", cur.lastrowid,
                  {"betreff": subject, "start": start, "anzahl": count})
    return cur.lastrowid


def get(ctx: Ctx, sid: int) -> dict:
    row = ctx.conn.execute("SELECT * FROM recurring_invoices WHERE id = ?", (sid,)).fetchone()
    if row is None:
        raise KontorError(f"Serie {sid} gibt es nicht.")
    s = dict(row)
    s["lines"] = json.loads(s["lines_json"])
    s["net_per_period"] = sum(l["total"] for l in s["lines"])
    s["customer"] = md.get_customer(ctx, s["customer_id"])
    return s


def list_all(ctx: Ctx) -> list[dict]:
    return [get(ctx, r[0]) for r in ctx.conn.execute(
        "SELECT id FROM recurring_invoices ORDER BY active DESC, next_date")]


def set_active(ctx: Ctx, sid: int, active: bool) -> None:
    get(ctx, sid)
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE recurring_invoices SET active = ? WHERE id = ?", (1 if active else 0, sid))
        audit.log(ctx.conn, ctx.actor, "serie_aktiv" if active else "serie_pausiert", "serie", sid, {})


def generate_due(ctx: Ctx, today: str | None = None) -> list[int]:
    """Für jede fällige Periode einen Rechnungsentwurf anlegen (auch nachgeholte). Gibt die neuen Dokument-IDs zurück."""
    day = date.fromisoformat(today) if today else ctx.now_date()
    created = []
    for r in ctx.conn.execute("SELECT id FROM recurring_invoices WHERE active = 1 ORDER BY id").fetchall():
        s = get(ctx, r[0])
        while s["active"] and date.fromisoformat(s["next_date"]) <= day:
            frm = date.fromisoformat(s["next_date"])
            nxt = add_months(frm, s["interval_months"])
            with tx(ctx.conn):
                did = docs.save_draft(ctx, {
                    "kind": "rechnung", "customer_id": s["customer_id"], "doc_date": day.isoformat(),
                    "service_from": frm.isoformat(), "service_to": (nxt - timedelta(days=1)).isoformat(),
                    "subject": f"{s['subject']} – {MONTHS[frm.month - 1]} {frm.year}",
                    "source_document_id": s["source_document_id"],
                    "lines": [{k: l[k] for k in ("title", "description", "quantity", "unit", "unit_price", "tax_rate")}
                              for l in s["lines"]]})
                issued = s["issued"] + 1
                active = 0 if s["count"] is not None and issued >= s["count"] else 1
                ctx.conn.execute("UPDATE recurring_invoices SET issued = ?, next_date = ?, active = ? WHERE id = ?",
                                 (issued, nxt.isoformat(), active, s["id"]))
                audit.log(ctx.conn, ctx.actor, "serie_rechnung", "serie", s["id"],
                          {"dokument": did, "zeitraum_ab": frm.isoformat()})
            created.append(did)
            s = get(ctx, s["id"])
    return created
