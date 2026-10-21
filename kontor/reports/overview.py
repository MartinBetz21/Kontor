"""Startseite: was rein- und rausging, was offen ist, was im laufenden Zeitraum an Steuer anfällt."""
from __future__ import annotations

from ..context import Ctx
from ..core.tax_codes import deadline, period_of
from ..services import documents as docs
from ..services import masterdata as md
from .euer import euer
from .ustva import ustva


def overview(ctx: Ctx) -> dict:
    today = ctx.now_date()
    year = str(today.year)
    months = []
    for m in range(1, today.month + 1):
        key = f"{year}-{m:02d}"
        row = ctx.conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN amount > 0 THEN amount END), 0), "
            "COALESCE(SUM(CASE WHEN amount < 0 THEN -amount END), 0) FROM payments WHERE substr(pay_date, 1, 7) = ?",
            (key,)).fetchone()
        months.append({"month": key, "in": row[0], "out": row[1]})
    open_invoices = [d for d in docs.list_documents(ctx, group="rechnungen")
                     if d["status"] in ("abgeschlossen", "teilbezahlt") and d["kind"] != "storno"]
    for d in open_invoices:
        d["overdue"] = bool(d["due_date"] and d["due_date"] < today.isoformat())
    drafts = docs.list_documents(ctx, status="entwurf")
    inbox = ctx.conn.execute("SELECT COUNT(*) FROM receipts WHERE status = 'eingang'").fetchone()[0]
    period = period_of(today, md.get_settings(ctx)["ustva_period"])
    from ..services.pipeline import open_followups
    return {"today": today.isoformat(), "followups": open_followups(ctx), "months": months, "open_invoices": open_invoices, "drafts": drafts,
            "inbox": inbox, "period": period, "estimate": ustva(ctx, period)["zahllast"],
            "deadline": deadline(period).isoformat(), "profit_ytd": euer(ctx, today.year)["gewinn"]}
