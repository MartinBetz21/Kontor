"""Abo-Übersicht: was läuft, was es im Monat und im Jahr kostet, was demnächst abgebucht wird.
Nur Übersicht; gebucht wird weiter über die Belege."""
from __future__ import annotations

from datetime import date, timedelta

from ..context import Ctx
from ..core.errors import KontorError
from ..core.tax_codes import TAX_KINDS
from ..store import audit
from ..store.db import tx
from . import masterdata as md
from .documents import _iso
from .series import add_months

FIELDS = ("name", "provider", "amount", "interval", "next_due", "payment_via", "tax_kind", "status", "ends_on",
          "cancel_info", "note")
INTERVALS = {"monat": "monatlich", "jahr": "jährlich"}


def _clean(data: dict, base: dict | None = None) -> dict:
    out = {k: (base or {}).get(k) for k in FIELDS}
    for k, v in data.items():
        if k not in FIELDS:
            continue
        if k == "amount":
            out[k] = None if v in (None, "") else md.to_cents(v)
        elif k in ("next_due", "ends_on"):
            out[k] = _iso(v, k)
        elif k == "tax_kind":
            out[k] = v or None
        else:
            out[k] = str(v or "").strip()
    out["interval"] = out["interval"] or "monat"
    out["status"] = out["status"] or "aktiv"
    for k in ("provider", "payment_via", "cancel_info", "note"):
        out[k] = out[k] or ""
    if not out["name"]:
        raise KontorError("Abo braucht einen Namen.")
    if out["interval"] not in INTERVALS:
        raise KontorError("Intervall ist „monat“ oder „jahr“.")
    if out["tax_kind"] and out["tax_kind"] not in TAX_KINDS:
        raise KontorError(f"Steuerart {out['tax_kind']!r} gibt es nicht.")
    if out["status"] not in ("aktiv", "gekuendigt"):
        raise KontorError("Status ist „aktiv“ oder „gekuendigt“.")
    return out


def create(ctx: Ctx, data: dict) -> int:
    s = _clean(data)
    with tx(ctx.conn):
        cur = ctx.conn.execute(f"INSERT INTO subscriptions ({', '.join(FIELDS)}) VALUES ({', '.join('?' * len(FIELDS))})",
                               [s[k] for k in FIELDS])
        audit.log(ctx.conn, ctx.actor, "abo_anlegen", "abo", cur.lastrowid, {"name": s["name"], "betrag": s["amount"]})
    return cur.lastrowid


def get(ctx: Ctx, sid: int) -> dict:
    row = ctx.conn.execute("SELECT * FROM subscriptions WHERE id = ?", (sid,)).fetchone()
    if row is None:
        raise KontorError(f"Abo {sid} gibt es nicht.")
    return dict(row)


def update(ctx: Ctx, sid: int, data: dict) -> None:
    s = _clean(data, get(ctx, sid))
    with tx(ctx.conn):
        ctx.conn.execute(f"UPDATE subscriptions SET {', '.join(k + ' = ?' for k in FIELDS)} WHERE id = ?",
                         [s[k] for k in FIELDS] + [sid])
        audit.log(ctx.conn, ctx.actor, "abo_aendern", "abo", sid, {k: v for k, v in data.items() if k in FIELDS})


def _next_display(s: dict, today: date) -> str | None:
    """Nächste Abbuchung ab heute; ein vergangenes Datum wird um das Intervall weitergerechnet (nur Anzeige)."""
    if not s["next_due"]:
        return None
    d = date.fromisoformat(s["next_due"])
    step = 1 if s["interval"] == "monat" else 12
    while d < today:
        d = add_months(d, step)
    return d.isoformat()


def per_month(s: dict) -> int:
    if s["amount"] is None or s["status"] != "aktiv":
        return 0
    return s["amount"] if s["interval"] == "monat" else round(s["amount"] / 12)


def list_all(ctx: Ctx) -> list[dict]:
    today = ctx.now_date()
    out = []
    for r in ctx.conn.execute("SELECT * FROM subscriptions ORDER BY status, name COLLATE NOCASE"):
        s = dict(r)
        s["next_display"] = _next_display(s, today) if s["status"] == "aktiv" else None
        s["per_month"] = per_month(s)
        out.append(s)
    return out


def overview(ctx: Ctx) -> dict:
    items = list_all(ctx)
    active = [s for s in items if s["status"] == "aktiv"]
    horizon = (ctx.now_date() + timedelta(days=30)).isoformat()
    per_year = sum((s["amount"] or 0) * (12 if s["interval"] == "monat" else 1) for s in active)
    return {"items": items, "per_month": sum(s["per_month"] for s in active), "per_year": per_year,
            "upcoming": sorted([s for s in active if s["next_display"] and s["next_display"] <= horizon],
                               key=lambda s: s["next_display"]),
            "missing_amount": [s for s in active if s["amount"] is None]}
