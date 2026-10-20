"""Buchungen, die Kontor selbst aus Rechnungen, Zahlungen und Bankzuordnungen ableitet (Spec 18.7a, Art `auto`).
Deterministisch: dieselben Daten ergeben dieselben Zeilen. Solange ein Jahr offen ist, werden sie bei jeder Abfrage neu
berechnet; beim Sperren schreibt journal.lock_year sie fest."""
from __future__ import annotations

import json
from decimal import Decimal

from ..context import Ctx
from ..core.money import round_cents
from ..core.status import INVOICE_KINDS

RC_RATE = Decimal("19")
REVENUE = {("inland", "19"): "4400", ("inland", "7"): "4300", ("inland", "0"): "4200",
           ("drittland_b2b", "0"): "4338", ("eu_b2b_rc", "0"): "4336"}
VAT_NOT_DUE = {"19": "3816", "7": "3811"}
VAT_DUE = {"19": "3806", "7": "3801"}
INPUT_VAT = {"19": "1406", "7": "1401"}
RC_INPUT, RC_OUTPUT = "1407", "3837"
UNASSIGNED = "3500"


def _row(day: str, account: str, amount: int, text: str, contra: str = "", beleg: str = "", ref: str = "") -> dict:
    return {"date": day, "account": account, "amount": amount, "contra": contra, "text": text, "beleg": beleg,
            "tax_rate": "", "tax_account": "", "art": "auto", "ref": ref}


def _invoices(ctx: Ctx, year: int) -> list[dict]:
    out = []
    for d in ctx.conn.execute(
            "SELECT * FROM documents WHERE kind IN (%s) AND status <> 'entwurf'" % ",".join("?" * len(INVOICE_KINDS)),
            list(INVOICE_KINDS)):
        if d["origin"] == "lexware" and d["status"] == "storniert":
            continue                                   # in Lexware storniert, ohne Stornobeleg: hebt sich auf
        day = d["service_to"] or d["service_date"] or d["doc_date"]
        if int(day[:4]) != year:
            continue                                   # Leistung in einem anderen Jahr (frühere stecken im Vortrag)
        snap = json.loads(d["snapshot_json"])
        treatment = snap.get("tax_treatment") or "inland"
        ref = d["number"]
        out.append(_row(day, "1200", d["gross"], f"{ref} {d['subject'] or 'Rechnung'}".strip(), beleg=ref, ref=ref))
        for g in snap["totals"]["groups"]:
            account = REVENUE.get((treatment, g["rate"]), "4400")
            out.append(_row(day, account, -g["basis"], f"{ref} Erlös {g['rate']} %", "1200", ref, ref))
            if g["tax"]:
                out.append(_row(day, VAT_NOT_DUE.get(g["rate"], "3816"), -g["tax"], f"{ref} USt nicht fällig", "1200", ref, ref))
    return out


def _payments(ctx: Ctx, year: int) -> list[dict]:
    """Ist-Versteuerung: mit der Zahlung wird die Umsatzsteuer fällig (3816 → 3806)."""
    out = []
    for p in ctx.conn.execute(
            "SELECT p.id, p.pay_date, d.number FROM payments p JOIN documents d ON d.id = p.document_id "
            "WHERE p.kind = 'einnahme' AND p.origin = 'ledger' AND substr(p.pay_date, 1, 4) = ?", (str(year),)):
        for part in ctx.conn.execute("SELECT tax_rate, tax FROM payment_parts WHERE payment_id = ?", (p["id"],)):
            if part["tax"]:
                rate = part["tax_rate"]
                out.append(_row(p["pay_date"], VAT_NOT_DUE.get(rate, "3816"), part["tax"], f"{p['number']} USt fällig",
                                VAT_DUE.get(rate, "3806"), p["number"], p["number"]))
                out.append(_row(p["pay_date"], VAT_DUE.get(rate, "3806"), -part["tax"], f"{p['number']} USt fällig",
                                VAT_NOT_DUE.get(rate, "3816"), p["number"], p["number"]))
    return out


def _bank(ctx: Ctx, year: int) -> list[dict]:
    out = []
    for t in ctx.conn.execute(
            "SELECT t.*, b.account AS bank, a.kind, a.account AS target, a.tax_key, a.receipt_id, d.number "
            "FROM bank_transactions t JOIN bank_accounts b ON b.iban = t.account "
            "LEFT JOIN bank_assignments a ON a.bank_transaction_id = t.id LEFT JOIN documents d ON d.id = a.document_id "
            "WHERE t.duplicate = 0 AND substr(t.booked_on, 1, 4) = ? ORDER BY t.booked_on, t.id", (str(year),)):
        g, day, text, ref = t["amount"], t["booked_on"], " ".join(f"{t['counterparty']} {t['purpose']}".split()), f"B{t['id']}"
        if t["kind"] is None:
            out += [_row(day, t["bank"], g, text, UNASSIGNED, ref=ref), _row(day, UNASSIGNED, -g, text, t["bank"], ref=ref)]
            continue
        if t["kind"] == "rechnung":
            out += [_row(day, t["bank"], g, text, "1200", t["number"], ref), _row(day, "1200", -g, text, t["bank"], t["number"], ref)]
            continue
        if t["kind"] == "intern":
            out += [_row(day, t["bank"], g, text, "1460", ref=ref), _row(day, "1460", -g, text, t["bank"], ref=ref)]
            continue
        out.append(_row(day, t["bank"], g, text, t["target"], ref=ref))
        key = t["tax_key"]
        if key in ("19", "7"):
            net, tax = _split(ctx, g, key, t["receipt_id"])
            tax_account = INPUT_VAT[key] if g < 0 else VAT_DUE[key]
            out += [_row(day, t["target"], -net, text, t["bank"], ref=ref), _row(day, tax_account, -tax, text, t["bank"], ref=ref)]
        else:
            out.append(_row(day, t["target"], -g, text, t["bank"], ref=ref))
            if key in ("rc_eu", "rc_drittland"):        # § 13b: Steuer und Vorsteuer gleich hoch, zahllastneutral
                rc = round_cents(Decimal(-g) * RC_RATE / 100)
                out += [_row(day, RC_INPUT, rc, f"§ 13b {text}", RC_OUTPUT, ref=ref),
                        _row(day, RC_OUTPUT, -rc, f"§ 13b {text}", RC_INPUT, ref=ref)]
    return out


def _split(ctx: Ctx, gross: int, key: str, receipt_id: int | None) -> tuple[int, int]:
    if receipt_id:
        r = ctx.conn.execute("SELECT net, tax FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
        if r and r["net"] is not None and r["tax"] is not None and r["net"] + r["tax"] == abs(gross):
            sign = -1 if gross < 0 else 1
            return sign * r["net"], sign * r["tax"]
    net = round_cents(Decimal(gross) * 100 / (100 + Decimal(key)))
    return net, gross - net


def auto_lines(ctx: Ctx, year: int) -> list[dict]:
    return _invoices(ctx, year) + _payments(ctx, year) + _bank(ctx, year)
