"""Zahlungseingänge zu Rechnungen, Erstattungen auf Stornos, Zahlungen an/vom Finanzamt.
Ausgaben-Zahlungen entstehen in receipts.py aus 'bezahlt am'."""
from __future__ import annotations

import json

from ..context import Ctx, require_human
from ..core.allocation import allocate, remaining_by_rate
from ..core.errors import KontorError
from ..core.money import fmt_eur
from ..core.status import payment_status
from ..store import audit
from ..store.db import tx
from . import documents as docs
from . import masterdata as md
from .documents import _iso


def record_payment(ctx: Ctx, document_id: int, pay_date: str, amount: int | str, note: str = "",
                   origin: str = "ledger") -> int:          # "ledger" = in diesem Programm entstanden (Wert aus Migration 0010)
    """origin 'lexware': schon in Lexware gebucht und erklärt, zählt in keiner Voranmeldung (Spec 18.3)."""
    d = docs.get_document(ctx, document_id)
    if d["kind"] == "angebot" or d["status"] == "entwurf":
        raise KontorError("Zahlungen gibt es nur zu abgeschlossenen Rechnungen.")
    day = _iso(pay_date, "Zahlungsdatum")
    if not day:
        raise KontorError("Zahlungsdatum fehlt.")
    cents = md.to_cents(amount)
    if cents == 0:
        raise KontorError("Betrag ist 0.")
    if d["kind"] == "storno":
        if cents > 0 or -cents > -d["open"]:
            raise KontorError(f"Erstattung höchstens {fmt_eur(-d['open'])}, als negativer Betrag.")
    else:
        if d["status"] == "storniert":
            raise KontorError("Die Rechnung ist storniert. Erstattungen auf der Stornorechnung erfassen.")
        if cents < 0:
            raise KontorError("Erstattungen auf der Stornorechnung erfassen.")
        if cents > d["open"]:
            raise KontorError(f"Zahlung {fmt_eur(cents)} übersteigt den offenen Betrag {fmt_eur(d['open'])}.")
    snap = json.loads(d["snapshot_json"])
    paid_parts = [dict(r) for r in ctx.conn.execute(
        "SELECT pp.tax_rate, pp.net, pp.tax FROM payment_parts pp JOIN payments p ON p.id = pp.payment_id "
        "WHERE p.document_id = ?", (document_id,))]
    parts = allocate(cents, remaining_by_rate(snap["totals"]["groups"], snap["prepaid"], paid_parts))
    with tx(ctx.conn):
        cur = ctx.conn.execute("INSERT INTO payments (pay_date, amount, kind, document_id, note, origin) "
                               "VALUES (?,?,?,?,?,?)", (day, cents, "einnahme", document_id, note, origin))
        for p in parts:
            ctx.conn.execute("INSERT INTO payment_parts (payment_id, tax_rate, net, tax) VALUES (?,?,?,?)",
                             (cur.lastrowid, p.rate, p.net, p.tax))
        if d["kind"] != "storno":
            docs._set_status(ctx, document_id, payment_status(d["due"], d["paid"] + cents))
        audit.log(ctx.conn, ctx.actor, "zahlung", "dokument", document_id,
                  {"datum": day, "betrag": cents, "teile": [p.__dict__ for p in parts]})
    return cur.lastrowid


def record_unbilled(ctx: Ctx, pay_date: str, amount: int | str, tax_rate: str, note: str) -> int:
    """Zahlungseingang, zu dem es noch keine abgeschlossene Rechnung gibt (z. B. Zahlungslink, Anschrift fehlt). Die
    Umsatzsteuer entsteht mit dem Eingang (Istversteuerung) und zählt sofort; die Rechnung übernimmt die Zahlung
    später mit `link_unbilled`. Die Notiz sagt, von wem und wofür."""
    from decimal import Decimal

    from ..core.money import round_cents
    day = _iso(pay_date, "Zahlungsdatum")
    cents = md.to_cents(amount)
    if not day or cents <= 0:
        raise KontorError("Datum und positiven Betrag angeben.")
    if tax_rate not in ("19", "7"):
        raise KontorError("Steuersatz 19 oder 7.")
    if not (note or "").strip():
        raise KontorError("Notiz angeben: von wem, wofür, warum noch keine Rechnung.")
    net = round_cents(Decimal(cents) * 100 / (100 + Decimal(tax_rate)))
    with tx(ctx.conn):
        cur = ctx.conn.execute("INSERT INTO payments (pay_date, amount, kind, note) VALUES (?,?,?,?)",
                               (day, cents, "einnahme", note.strip()))
        ctx.conn.execute("INSERT INTO payment_parts (payment_id, tax_rate, net, tax) VALUES (?,?,?,?)",
                         (cur.lastrowid, tax_rate, net, cents - net))
        audit.log(ctx.conn, ctx.actor, "zahlung_ohne_rechnung", "zahlung", cur.lastrowid,
                  {"datum": day, "betrag": cents, "steuersatz": tax_rate, "notiz": note.strip()})
    return cur.lastrowid


def unbilled(ctx: Ctx) -> list[dict]:
    """Zahlungseingänge, zu denen die Rechnung noch fehlt."""
    return [dict(r) for r in ctx.conn.execute(
        "SELECT * FROM payments WHERE kind = 'einnahme' AND document_id IS NULL AND bank_transaction_id IS NULL "
        "AND origin = 'ledger' ORDER BY pay_date, id")]


def link_unbilled(ctx: Ctx, payment_id: int, document_id: int) -> None:
    """Die abgeschlossene Rechnung übernimmt eine Zahlung ohne Rechnung. Betrag und Steueraufteilung müssen zu dem
    passen, was die Rechnung für diesen Betrag ergäbe; die schon angemeldete Steuer ändert sich dadurch nicht."""
    row = ctx.conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if row is None or row["kind"] != "einnahme":
        raise KontorError(f"Zahlungseingang {payment_id} gibt es nicht.")
    if row["document_id"] is not None:
        raise KontorError(f"Zahlung {payment_id} gehört schon zu einer Rechnung.")
    d = docs.get_document(ctx, document_id)
    if d["kind"] in ("angebot", "storno") or d["status"] in ("entwurf", "storniert"):
        raise KontorError("Nur eine abgeschlossene, nicht stornierte Rechnung kann die Zahlung übernehmen.")
    if row["amount"] > d["open"]:
        raise KontorError(f"Zahlung {fmt_eur(row['amount'])} übersteigt den offenen Betrag {fmt_eur(d['open'])}.")
    snap = json.loads(d["snapshot_json"])
    paid_parts = [dict(r) for r in ctx.conn.execute(
        "SELECT pp.tax_rate, pp.net, pp.tax FROM payment_parts pp JOIN payments p ON p.id = pp.payment_id "
        "WHERE p.document_id = ?", (document_id,))]
    soll = sorted((p.rate, p.net, p.tax) for p in allocate(row["amount"], remaining_by_rate(
        snap["totals"]["groups"], snap["prepaid"], paid_parts)))
    ist = sorted((r["tax_rate"], r["net"], r["tax"]) for r in ctx.conn.execute(
        "SELECT tax_rate, net, tax FROM payment_parts WHERE payment_id = ?", (payment_id,)))
    if soll != ist:
        raise KontorError("Steueraufteilung der Zahlung passt nicht zur Rechnung (Steuersatz oder Rundung).")
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE payments SET document_id = ? WHERE id = ?", (document_id, payment_id))
        if d["kind"] != "storno":
            docs._set_status(ctx, document_id, payment_status(d["due"], d["paid"] + row["amount"]))
        audit.log(ctx.conn, ctx.actor, "zahlung_verknuepfen", "dokument", document_id,
                  {"zahlung": payment_id, "betrag": row["amount"]})


def record_tax_payment(ctx: Ctx, pay_date: str, amount: int | str, note: str = "") -> int:
    day = _iso(pay_date, "Zahlungsdatum")
    cents = md.to_cents(amount)
    if not day or cents == 0:
        raise KontorError("Datum und Betrag angeben (negativ = an das Finanzamt gezahlt).")
    with tx(ctx.conn):
        cur = ctx.conn.execute("INSERT INTO payments (pay_date, amount, kind, note) VALUES (?,?,?,?)",
                               (day, cents, "ust", note))
        audit.log(ctx.conn, ctx.actor, "ust_zahlung", "zahlung", cur.lastrowid, {"datum": day, "betrag": cents})
    return cur.lastrowid


def delete_payment(ctx: Ctx, payment_id: int, reason: str) -> None:
    """Falsch erfasste Zahlung entfernen (nur der Mensch, mit Begründung im Protokoll). Gesperrte Monate blockiert der
    Trigger. Zahlungen zu Belegen ändern sich über „bezahlt am“ am Beleg. Ausnahme: Eine Zahlung, die eine
    Bankzuordnung oder eine bestätigte Bitcoin-Kundenzahlung erzeugt hat, verschwindet mit dem Lösen dieser Zuordnung
    bzw. dem Zurücksetzen der Bewegung zum Entwurf (Spec 18.7a, 18.14), auch über die Kommandozeile."""
    if not (reason or "").strip():
        raise KontorError("Zum Löschen einer Zahlung eine Begründung angeben.")
    row = ctx.conn.execute("SELECT * FROM payments WHERE id = ?", (payment_id,)).fetchone()
    if row is None:
        raise KontorError(f"Zahlung {payment_id} gibt es nicht.")
    bank_released = row["source"] == "bank" and row["bank_transaction_id"] is not None and not ctx.conn.execute(
        "SELECT 1 FROM bank_assignments WHERE bank_transaction_id = ?", (row["bank_transaction_id"],)).fetchone()
    btc_released = ctx.conn.execute("SELECT 1 FROM btc_movements WHERE payment_id = ? AND status = 'entwurf'",
                                    (payment_id,)).fetchone()
    if not (bank_released or btc_released):
        require_human(ctx)
    if row["kind"] == "ausgabe":
        raise KontorError("Zahlungen zu Belegen ändern sich über „bezahlt am“ am Beleg.")
    with tx(ctx.conn):
        ctx.conn.execute("DELETE FROM payments WHERE id = ?", (payment_id,))
        if row["document_id"]:
            d = docs.get_document(ctx, row["document_id"])
            if d["kind"] != "storno" and d["status"] in ("abgeschlossen", "teilbezahlt", "bezahlt"):
                docs._set_status(ctx, d["id"], payment_status(d["due"], d["paid"]))
        audit.log(ctx.conn, ctx.actor, "zahlung_loeschen", "zahlung", payment_id,
                  {"datum": row["pay_date"], "betrag": row["amount"], "dokument": row["document_id"],
                   "begruendung": reason})


def list_payments(ctx: Ctx, document_id: int | None = None, year: int | None = None) -> list[dict]:
    sql, args = "SELECT * FROM payments WHERE 1=1", []
    if document_id:
        sql += " AND document_id = ?"
        args.append(document_id)
    if year:
        sql += " AND substr(pay_date, 1, 4) = ?"
        args.append(str(year))
    return [dict(r) for r in ctx.conn.execute(sql + " ORDER BY pay_date, id", args)]
