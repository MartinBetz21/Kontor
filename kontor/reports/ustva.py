"""Werte für die Umsatzsteuer-Voranmeldung, zum Eintragen in Mein ELSTER. Umsatzsteuer nach Zahlungseingang
(Istversteuerung), Vorsteuer nach Rechnungsdatum (§ 15 UStG: Rechnung liegt vor, Leistung erbracht)."""
from __future__ import annotations

from ..context import Ctx
from ..core.money import euro_floor
from ..core.tax_codes import KZ_LABELS, deadline, period_bounds
from ..services import masterdata as md


def _from_bank(ctx: Ctx, a: str, b: str, exact: dict) -> tuple[list[dict], list[dict]]:
    """Bilanz-Mandanten (Spec 18.7a): Vorsteuer, Reverse Charge und Umsatzsteuer aus Bankzuordnungen ohne Rechnung
    kommen aus der Bank, nicht aus EÜR-Belegen. Datum ist das Belegdatum, wenn ein Beleg verknüpft ist, sonst die
    Abbuchung. Rechnungen und ihre Zahlungen laufen wie bei der EÜR über payment_parts (Ist-Versteuerung)."""
    from decimal import Decimal

    from ..core.money import round_cents
    from ..services.autojournal import _split
    vorsteuer, rc = [], []
    for t in ctx.conn.execute(
            "SELECT t.*, a.kind, a.tax_key, a.receipt_id, COALESCE(r.receipt_date, t.booked_on) AS tag, "
            "COALESCE(r.supplier, t.counterparty) AS supplier FROM bank_transactions t "
            "JOIN bank_assignments a ON a.bank_transaction_id = t.id LEFT JOIN receipts r ON r.id = a.receipt_id "
            "WHERE t.duplicate = 0 AND a.kind IN ('konto', 'beleg')").fetchall():
        if not (a <= t["tag"] <= b):
            continue
        key, g = t["tax_key"], t["amount"]
        if key in ("19", "7"):
            net, tax = _split(ctx, g, key, t["receipt_id"])
            if g < 0:
                vorsteuer.append({"id": t["receipt_id"], "receipt_date": t["tag"], "supplier": t["supplier"],
                                  "net": -net, "tax": -tax})
            else:
                exact[key]["net"] += net
                exact[key]["tax"] += tax
        elif key in ("rc_eu", "rc_drittland") and g < 0:
            rc.append({"id": t["receipt_id"], "receipt_date": t["tag"], "supplier": t["supplier"], "net": -g,
                       "tax": round_cents(Decimal(-g) * 19 / 100), "tax_kind": key})
    return vorsteuer, rc


def ustva(ctx: Ctx, spec: str) -> dict:
    start, end = period_bounds(spec)
    a, b = start.isoformat(), end.isoformat()
    exact = {"19": {"net": 0, "tax": 0}, "7": {"net": 0, "tax": 0}}
    for r in ctx.conn.execute(
            "SELECT pp.tax_rate, SUM(pp.net) AS net, SUM(pp.tax) AS tax FROM payment_parts pp "
            "JOIN payments p ON p.id = pp.payment_id WHERE p.kind = 'einnahme' AND p.origin = 'ledger' "
            "AND p.pay_date BETWEEN ? AND ? "
            "GROUP BY pp.tax_rate", (a, b)):
        if r["tax_rate"] in exact:
            exact[r["tax_rate"]] = {"net": r["net"], "tax": r["tax"]}
    einnahmen = [dict(r) for r in ctx.conn.execute(
        "SELECT p.pay_date, p.amount, d.number, d.customer_json FROM payments p JOIN documents d ON d.id = p.document_id "
        "WHERE p.kind = 'einnahme' AND p.origin = 'ledger' AND p.pay_date BETWEEN ? AND ? ORDER BY p.pay_date", (a, b))]
    # Nicht steuerbar (Kz 45) und EU-Reverse-Charge (Kz 21): im Monat der Leistung, nicht der Zahlung (Spec 18.4)
    ausland = [dict(r) for r in ctx.conn.execute(
        "SELECT number, tax_treatment, net, customer_json, COALESCE(service_to, service_date, doc_date) AS leistung "
        "FROM documents WHERE status <> 'entwurf' AND kind <> 'angebot' AND NOT (origin = 'lexware' AND status = 'storniert') "
        "AND tax_treatment IN ('drittland_b2b', 'eu_b2b_rc') "
        "AND COALESCE(service_to, service_date, doc_date) BETWEEN ? AND ? ORDER BY leistung", (a, b))]
    vorsteuer = [dict(r) for r in ctx.conn.execute(
        "SELECT r.id, r.receipt_date, r.supplier, SUM(p.net) AS net, SUM(p.tax) AS tax FROM receipt_parts p "
        "JOIN receipts r ON r.id = p.receipt_id WHERE r.status = 'geprueft' AND p.tax_kind IN ('inland_19', 'inland_7') "
        "AND r.receipt_date BETWEEN ? AND ? GROUP BY r.id ORDER BY r.receipt_date", (a, b))]
    rc = [dict(r) for r in ctx.conn.execute(
        "SELECT r.id, r.receipt_date, r.supplier, p.net, p.tax, p.tax_kind FROM receipt_parts p "
        "JOIN receipts r ON r.id = p.receipt_id WHERE r.status = 'geprueft' AND p.tax_kind IN ('rc_eu', 'rc_drittland') "
        "AND r.receipt_date BETWEEN ? AND ? ORDER BY r.receipt_date", (a, b))]

    if md.get_profile(ctx)["gewinnermittlung"] == "bilanz":
        vorsteuer, rc = _from_bank(ctx, a, b, exact)

    b81, b86 = euro_floor(exact["19"]["net"]), euro_floor(exact["7"]["net"])
    rc_eu = [r for r in rc if r["tax_kind"] == "rc_eu"]
    rc_dl = [r for r in rc if r["tax_kind"] == "rc_drittland"]
    t47, t85 = sum(r["tax"] for r in rc_eu), sum(r["tax"] for r in rc_dl)
    v66 = sum(r["tax"] for r in vorsteuer)
    fields = {
        "81": {"basis": b81, "tax": b81 * 19},
        "86": {"basis": b86, "tax": b86 * 7},
        "46": {"basis": euro_floor(sum(r["net"] for r in rc_eu)), "tax": None},
        "47": {"basis": None, "tax": t47},
        "84": {"basis": euro_floor(sum(r["net"] for r in rc_dl)), "tax": None},
        "85": {"basis": None, "tax": t85},
        "45": {"basis": euro_floor(sum(r["net"] for r in ausland if r["tax_treatment"] == "drittland_b2b")),
               "tax": None},
        "21": {"basis": euro_floor(sum(r["net"] for r in ausland if r["tax_treatment"] == "eu_b2b_rc")), "tax": None},
        "66": {"basis": None, "tax": v66},
        "67": {"basis": None, "tax": t47 + t85},
    }
    zahllast = fields["81"]["tax"] + fields["86"]["tax"] + t47 + t85 - v66 - (t47 + t85)
    fields["83"] = {"basis": None, "tax": zahllast}
    for kz, f in fields.items():
        f["label"] = KZ_LABELS[kz]
    return {"period": spec, "from": a, "to": b, "deadline": deadline(spec).isoformat(), "fields": fields,
            "zahllast": zahllast, "exact": exact,
            "details": {"einnahmen": einnahmen, "vorsteuer": vorsteuer, "reverse_charge": rc, "ausland": ausland}}


def jahresuebersicht(ctx: Ctx, year: int) -> dict:
    """Umsatzsteuer-Übersicht je Jahr (Spec 7): wie viel dem Finanzamt gehört und auf dem Konto bleiben muss.
    Einbehalten ist centgenau und ohne Abzug; die Zahllast ist eine Prognose, ELSTER rundet die Bemessungsgrundlagen."""
    from datetime import date

    from ..core.tax_codes import period_of
    from ..services import periods as per
    locked = {l["month"] for l in per.locked_months(ctx)}
    rows = []
    for spec in dict.fromkeys(period_of(date(year, m, 1), md.get_settings(ctx)["ustva_period"]) for m in range(1, 13)):
        u = ustva(ctx, spec)
        ein = u["exact"]["19"]["tax"] + u["exact"]["7"]["tax"]
        vst = u["fields"]["66"]["tax"]
        start, end = period_bounds(spec)
        months = {f"{year}-{m:02d}" for m in range(start.month, end.month + 1)}
        rows.append({"period": spec, "einbehalten": ein, "vorsteuer": vst, "zahllast": ein - vst,
                     "reverse_charge": u["fields"]["67"]["tax"], "deadline": u["deadline"],
                     "gesperrt": months <= locked})
    paid = -ctx.conn.execute("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE kind = 'ust' "
                             "AND substr(pay_date, 1, 4) = ?", (str(year),)).fetchone()[0]
    ein, vst = sum(r["einbehalten"] for r in rows), sum(r["vorsteuer"] for r in rows)
    return {"year": year, "einbehalten": ein, "vorsteuer": vst, "zahllast": ein - vst, "gezahlt": paid,
            "offen": ein - vst - paid, "reverse_charge": sum(r["reverse_charge"] for r in rows), "zeitraeume": rows}
