"""EÜR nach Zahlungsfluss. Die Reverse-Charge-Steuer ist zahlungsneutral (Steuer = Vorsteuer) und fehlt hier bewusst;
der Nettobetrag zählt als Ausgabe. Bewirtung: 70 % abziehbar, 30 % getrennt ausgewiesen und nicht im Gewinn."""
from __future__ import annotations

from decimal import Decimal

from ..context import Ctx
from ..core.money import round_cents


def euer(ctx: Ctx, year: int) -> dict:
    y = str(year)
    inc = ctx.conn.execute(
        "SELECT COALESCE(SUM(pp.net), 0), COALESCE(SUM(pp.tax), 0) FROM payment_parts pp "
        "JOIN payments p ON p.id = pp.payment_id WHERE p.kind = 'einnahme' AND p.origin = 'ledger' AND substr(p.pay_date, 1, 4) = ?",
        (y,)).fetchone()
    ust = ctx.conn.execute(
        "SELECT COALESCE(SUM(CASE WHEN amount > 0 THEN amount END), 0), "
        "COALESCE(SUM(CASE WHEN amount < 0 THEN -amount END), 0) FROM payments "
        "WHERE kind = 'ust' AND substr(pay_date, 1, 4) = ?", (y,)).fetchone()
    rows = [dict(r) for r in ctx.conn.execute(
        "SELECT COALESCE(c.name, 'ohne Kategorie') AS category, SUM(p.net) AS net FROM receipt_parts p "
        "JOIN receipts r ON r.id = p.receipt_id LEFT JOIN categories c ON c.id = r.category_id "
        "WHERE r.status = 'geprueft' AND substr(r.paid_on, 1, 4) = ? GROUP BY category ORDER BY category", (y,))]
    ausgaben = []
    for a in rows:
        if a["category"] == "Bewirtung":       # § 4 Abs. 5 Nr. 2 EStG: 70 % abziehbar, Vorsteuer voll
            ab = round_cents(Decimal(a["net"]) * 7 / 10)
            ausgaben += [{"category": "Bewirtung (70 % abziehbar)", "net": ab},
                         {"category": "Bewirtung (30 % nicht abziehbar)", "net": a["net"] - ab,
                          "nicht_abziehbar": True}]
        else:
            ausgaben.append(a)
    vorsteuer = ctx.conn.execute(
        "SELECT COALESCE(SUM(p.tax), 0) FROM receipt_parts p JOIN receipts r ON r.id = p.receipt_id "
        "WHERE r.status = 'geprueft' AND p.tax_kind IN ('inland_19', 'inland_7') AND substr(r.paid_on, 1, 4) = ?",
        (y,)).fetchone()[0]
    out_net = sum(a["net"] for a in ausgaben if not a.get("nicht_abziehbar"))
    result = {"year": year, "einnahmen_netto": inc[0], "ust_vereinnahmt": inc[1], "ust_erstattet": ust[0],
              "ausgaben": ausgaben, "ausgaben_netto": out_net, "vorsteuer": vorsteuer, "ust_an_fa": ust[1]}
    result["gewinn"] = (inc[0] + inc[1] + ust[0]) - (out_net + vorsteuer + ust[1])
    return result
