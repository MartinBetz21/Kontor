"""Offene Posten aus Lexware übernehmen (Spec 18.3). Liest die JSON-Antworten der Lexware-API (`/invoices/{id}`,
`/payments/{id}`) und das Lexware-PDF; der Abruf selbst liegt außerhalb des Codes, damit hier kein Schlüssel steht.

Die Rechnung behält ihre Lexware-Nummer und ihr PDF, verbraucht keine neue Nummer und gilt als abgeschlossen.
Zahlungen aus der Lexware-Zeit tragen origin 'lexware' und zählen in keiner Voranmeldung, denn sie sind schon
erklärt. Was nach der Übernahme eingeht, zählt normal (Ist-Versteuerung)."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from decimal import Decimal

from ..context import Ctx
from ..core.calc import Allowance, Line, compute
from ..core.errors import KontorError
from ..core.snapshot import build_snapshot
from ..services import documents as docs
from ..services import masterdata as md
from ..services import payments as pay
from ..store import audit
from ..store.db import tx

TREATMENTS = {"net": "inland", "gross": "inland", "thirdPartyCountryService": "drittland_b2b"}
LINE_TYPES = ("custom", "service", "material")


def _cents(euro) -> int:
    return int((Decimal(str(euro)) * 100).to_integral_value())


def _day(iso: str | None) -> str | None:
    return iso[:10] if iso else None


def _buyer(address: dict) -> dict:
    """Empfänger so, wie er auf der Lexware-Rechnung steht (nicht der heutige Kundenstamm)."""
    return {"company": (address.get("name") or "").strip(), "contact": address.get("contactPerson") or "",
            "street": address.get("street") or "", "postcode": address.get("zip") or "",
            "city": (address.get("city") or "").strip(), "country": address.get("countryCode") or "DE"}


def _customer(ctx: Ctx, address: dict) -> int:
    """Erst über die Lexware-Kontakt-ID (Namen ändern sich, z. B. nach einer Gründung), dann Firmenname und Ort."""
    contact = address.get("contactId") or ""
    buyer = _buyer(address)
    for c in md.list_customers(ctx):
        if contact and contact in (c.get("note") or ""):
            return c["id"]
    for c in md.list_customers(ctx):
        if (c["company"] or "").strip().lower() == buyer["company"].lower() \
                and (c["city"] or "").strip().lower() == buyer["city"].lower():
            return c["id"]
    return md.create_customer(ctx, {**buyer, "note": f"aus Lexware übernommen; Lexware-Kontakt {contact}".strip()})


def import_invoice(ctx: Ctx, invoice: dict, payments: dict, pdf: bytes, with_payments: bool = True) -> int:
    """with_payments=False: Zahlungen kommen aus dem Bankimport (Spec 18.7a), nicht aus Lexware.
    In Lexware stornierte Rechnungen (voided) kommen als „storniert“; das Journal übergeht sie."""
    number = invoice.get("voucherNumber") or ""
    if not number:
        raise KontorError("Lexware-Rechnung ohne Nummer (Entwurf?) wird nicht übernommen.")
    if ctx.conn.execute("SELECT 1 FROM documents WHERE number = ?", (number,)).fetchone():
        raise KontorError(f"{number} gibt es im Kontor schon.")
    tax_type = (invoice.get("taxConditions") or {}).get("taxType", "net")
    if tax_type not in TREATMENTS:
        raise KontorError(f"{number}: Lexware-Steuerart {tax_type!r} wird noch nicht übernommen.")
    lines = [{"title": l.get("name") or "", "description": l.get("description") or "",
              "quantity": str(l.get("quantity") or 1), "unit": l.get("unitName") or "",
              "unit_price": _cents(l["unitPrice"]["netAmount"]),
              "tax_rate": str(int(l["unitPrice"].get("taxRatePercentage") or 0))}
             for l in invoice.get("lineItems", []) if l.get("type") in LINE_TYPES]
    totals = compute([Line(l["quantity"], l["unit_price"], l["tax_rate"]) for l in lines], [])
    expected = invoice.get("totalPrice") or {}
    if (totals.net, totals.gross) != (_cents(expected.get("totalNetAmount", 0)), _cents(expected.get("totalGrossAmount", 0))):
        raise KontorError(f"{number}: Summe weicht von Lexware ab (Kontor {totals.net}/{totals.gross} Cent, "
                          f"Lexware {expected.get('totalNetAmount')}/{expected.get('totalGrossAmount')} €).")
    ship = invoice.get("shippingConditions") or {}
    period = ship.get("shippingType") == "deliveryperiod" and ship.get("shippingEndDate")
    title = (invoice.get("title") or "").strip()
    data = {"kind": "rechnung", "tax_treatment": TREATMENTS[tax_type],
            "customer_id": _customer(ctx, invoice.get("address") or {}),
            "doc_date": _day(invoice.get("voucherDate")), "due_date": _day(invoice.get("dueDate")),
            "service_date": None if period else _day(ship.get("shippingDate")),
            "service_from": _day(ship.get("shippingDate")) if period else None,
            "service_to": _day(ship.get("shippingEndDate")) if period else None,
            "subject": "" if title == "Rechnung" else title, "intro": invoice.get("introduction") or "",
            "closing": invoice.get("remark") or "", "notes": "", "payment_note": "", "lines": lines}
    year = data["doc_date"][:4]
    folder = ctx.data / "archiv" / year
    pdf_file, json_file = folder / f"{number}.pdf", folder / f"{number}.json"
    if pdf_file.exists() or json_file.exists():
        raise KontorError(f"Im Archiv liegt schon {pdf_file.name}.")
    written = []
    try:
        with tx(ctx.conn):
            did = docs.save_draft(ctx, data)
            doc = docs.get_document(ctx, did)
            customer = {**md.get_customer(ctx, doc["customer_id"]), **_buyer(invoice.get("address") or {})}
            doc["customer"] = customer
            snap = build_snapshot(doc, md.get_settings(ctx), number, [], None)
            snap["origin"] = "lexware"
            snapshot_json = json.dumps(snap, ensure_ascii=False, indent=1)
            folder.mkdir(parents=True, exist_ok=True)
            pdf_file.write_bytes(pdf)
            written.append(pdf_file)
            json_file.write_text(snapshot_json, encoding="utf-8")
            written.append(json_file)
            sha = hashlib.sha256(pdf).hexdigest()
            ctx.conn.execute(
                "UPDATE documents SET number = ?, customer_json = ?, snapshot_json = ?, pdf_path = ?, sha256 = ?, "
                "finalized_at = ?, status = 'abgeschlossen', origin = 'lexware' WHERE id = ?",
                (number, json.dumps(customer, ensure_ascii=False), snapshot_json, str(pdf_file.relative_to(ctx.data)),
                 sha, datetime.now().isoformat(timespec="seconds"), did))
            audit.log(ctx.conn, ctx.actor, "lexware_uebernommen", "dokument", did,
                      {"nummer": number, "brutto": totals.gross, "sha256": sha})
            if invoice.get("voucherStatus") == "voided":
                ctx.conn.execute("UPDATE documents SET status = 'storniert' WHERE id = ?", (did,))
            for item in (payments.get("paymentItems", []) if with_payments else []):
                amount = _cents(item.get("amount") or 0)
                if amount > 0:
                    pay.record_payment(ctx, did, _day(item.get("postingDate")), amount,
                                       f"Lexware: {item.get('paymentItemType', '')}", origin="lexware")
    except BaseException:
        for f in written:
            f.unlink(missing_ok=True)
        raise
    for f in written:
        os.chmod(f, 0o444)
    return did


# -- Kontenexport und Saldenliste (Spec 18.8a) ----------------------------------------------------------------
PERSONAL_ACCOUNT = re.compile(r"^(1|7)0\d{3}$")      # Debitoren 10xxx, Kreditoren 70xxx: Unterkonten von 1200/3300


def _de_cents(text: str) -> int:
    t = (text or "0").strip().replace(".", "").replace(",", ".")
    return int((Decimal(t or "0") * 100).to_integral_value())


def _split(line: str) -> tuple[list[str], bool]:
    out, cur, quoted = [], "", False
    for ch in line:
        if ch == '"':
            quoted = not quoted
            continue
        if ch == ";" and not quoted:
            out.append(cur)
            cur = ""
            continue
        cur += ch
    out.append(cur)
    return out, quoted


def parse_kontenuebersicht(text: str) -> list[dict]:
    """Lexware → Buchhaltung → Kontenübersicht (CSV). Lange Texte enthalten Zeilenumbrüche INNERHALB eines Feldes;
    ein Datensatz ist erst vollständig, wenn die Anführungszeichen aufgehen."""
    lines = text.lstrip("﻿").replace("\r\n", "\n").split("\n")
    rows, buf = [], ""
    for line in lines[1:]:
        buf = buf + line if buf else line
        fields, open_ = _split(buf)
        if open_ or (buf and len(fields) < 10):
            buf += "\n"
            continue
        buf = ""
        if not fields or not fields[0][:1].isdigit():
            continue
        konto, name, datum, beleg, txt, gegen, soll, haben, steuer, steuerkonto = fields[:10]
        if PERSONAL_ACCOUNT.match(konto):
            continue
        d, m, y = datum.split(".")
        rate = steuer.replace(",00", "").strip()
        rows.append({"account": konto, "name": name, "date": f"{y}-{m}-{d}", "beleg": beleg,
                     "text": " ".join(txt.split()), "contra": gegen, "amount": _de_cents(soll) - _de_cents(haben),
                     "tax_rate": "" if rate in ("", "0") else rate, "tax_account": steuerkonto})
    return rows


def parse_saldenliste(text: str) -> tuple[dict[str, int], dict[str, str]]:
    """Saldenliste im Format Konto;Bezeichnung;Saldo;S/H (z. B. aus Lexware)."""
    bal, names = {}, {}
    for line in text.lstrip("﻿").splitlines()[1:]:
        if not line.strip():
            continue
        konto, name, saldo, sh = (c.strip() for c in line.split(";")[:4])
        cents = _de_cents(saldo)
        bal[konto] = cents if sh.upper() == "S" else -cents
        names[konto] = name
    return bal, names


# -- Eingangsbelege (Spec 18.7a) ---------------------------------------------------------------------------------
RATE_KIND = {19: "inland_19", 7: "inland_7", 0: "ohne_vorsteuer"}


def import_purchase_voucher(ctx: Ctx, voucher: dict, content: bytes, filename: str) -> int:
    """Eingangsbeleg aus Lexware (`/vouchers/{id}` + Datei) als geprüfter Beleg: In Lexware ist er bereits erfasst.
    Die Steuer übernimmt Kontor so, wie Lexware sie erfasst hat; ob sie stimmt, prüft die Auffälligkeit
    `steuer_beleg_abweichend` gegen die Bankzuordnung."""
    from ..services import receipts as rc
    ref = f"lexware:{voucher['id']}"
    row = ctx.conn.execute("SELECT id FROM receipts WHERE source_ref = ?", (ref,)).fetchone()
    if row:
        return row[0]
    gross, tax = _cents(voucher.get("totalGrossAmount") or 0), _cents(voucher.get("totalTaxAmount") or 0)
    parts: dict[str, list[int]] = {}
    for item in voucher.get("voucherItems") or []:
        kind = RATE_KIND.get(int(item.get("taxRatePercent") or 0), "ohne_vorsteuer")
        g, t = _cents(item.get("amount") or 0), _cents(item.get("taxAmount") or 0)
        if voucher.get("taxType") == "net":
            g += t
        parts.setdefault(kind, [0, 0])
        parts[kind][0] += g - t
        parts[kind][1] += t
    result = rc.add_file(ctx, content, filename, "cli", "aus Lexware übernommen", ref)
    rid = result["id"]
    day = (voucher.get("voucherDate") or "")[:10]
    supplier = voucher.get("contactName") or voucher.get("_contactName") or "Lieferant aus Lexware"
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE receipts SET status = 'geprueft', receipt_date = ?, supplier = ?, receipt_number = ?, "
                         "net = ?, tax = ?, gross = ?, tax_kind = ?, source_ref = ? WHERE id = ?",
                         (day, supplier, voucher.get("voucherNumber") or "", gross - tax, tax, gross,
                          next(iter(parts)) if len(parts) == 1 else None, ref, rid))
        ctx.conn.execute("DELETE FROM receipt_parts WHERE receipt_id = ?", (rid,))
        for kind, (net, t) in parts.items():
            ctx.conn.execute("INSERT INTO receipt_parts (receipt_id, tax_kind, net, tax) VALUES (?, ?, ?, ?)",
                             (rid, kind, net, t))
        rc._file_to_month(ctx, rc.get_receipt(ctx, rid))
        audit.log(ctx.conn, ctx.actor, "lexware_beleg_uebernommen", "beleg", rid,
                  {"lexware": voucher["id"], "brutto": gross, "steuer": tax})
    return rid
