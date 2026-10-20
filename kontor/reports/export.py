"""Journal als CSV (Semikolon, deutsche Beträge) für den Steuerberater."""
from __future__ import annotations

import csv
import io
import json

from ..context import Ctx
from ..core.money import fmt_eur


def _eur(cents: int | None) -> str:
    return "" if cents is None else fmt_eur(cents).replace(" €", "")


def journal_csv(ctx: Ctx, year: int) -> str:
    out = io.StringIO()
    w = csv.writer(out, delimiter=";", lineterminator="\n")
    w.writerow(["Datum", "Art", "Beleg", "Partner", "Netto", "Steuer", "Brutto", "Steuer/Satz", "Kategorie", "Notiz"])
    y = str(year)
    for p in ctx.conn.execute(
            "SELECT p.*, d.number, d.customer_json FROM payments p LEFT JOIN documents d ON d.id = p.document_id "
            "WHERE p.kind IN ('einnahme', 'ust') AND substr(p.pay_date, 1, 4) = ? ORDER BY p.pay_date, p.id", (y,)):
        if p["kind"] == "ust":
            w.writerow([p["pay_date"], "Finanzamt", "", "Finanzamt", "", "", _eur(p["amount"]), "USt", "", p["note"]])
            continue
        partner = json.loads(p["customer_json"] or "{}")
        for part in ctx.conn.execute("SELECT * FROM payment_parts WHERE payment_id = ?", (p["id"],)):
            w.writerow([p["pay_date"], "Einnahme", p["number"], partner.get("company") or partner.get("contact", ""),
                        _eur(part["net"]), _eur(part["tax"]), _eur(part["net"] + part["tax"]),
                        f"{part['tax_rate']} %", "", p["note"]])
    for r in ctx.conn.execute(
            "SELECT r.*, c.name AS category FROM receipts r LEFT JOIN categories c ON c.id = r.category_id "
            "WHERE r.status = 'geprueft' AND substr(COALESCE(r.paid_on, r.receipt_date), 1, 4) = ? "
            "ORDER BY COALESCE(r.paid_on, r.receipt_date)", (y,)):
        w.writerow([r["paid_on"] or r["receipt_date"], "Ausgabe", r["receipt_number"] or f"Beleg {r['id']}",
                    r["supplier"], _eur(-r["net"]), _eur(-r["tax"]), _eur(-r["gross"]), r["tax_kind"] or "gemischt",
                    r["category"] or "",
                    "; ".join(filter(None, [r["note"], r["bewirtung_anlass"] and f"Anlass: {r['bewirtung_anlass']}",
                                            r["bewirtung_teilnehmer"] and f"Bewirtete: {r['bewirtung_teilnehmer']}"]))])
    return out.getvalue()
