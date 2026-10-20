"""Der Schnappschuss ist die Wahrheit eines abgeschlossenen Dokuments (format_version 1).
PDF, XML und spätere Anzeige stützen sich darauf, nie auf aktuelle Stammdaten."""
from __future__ import annotations

import re

from .calc import Allowance, Line, compute
from .money import fmt_eur
from .tax_codes import TREATMENT_NOTES

FORMAT_VERSION = 1
SELLER_KEYS = ("name", "trade_name", "street", "postcode", "city", "country", "email", "phone",
               "tax_number", "tax_number_pending", "vat_id", "iban", "bic", "bank_name",
               "seat", "register_court", "register_number", "managing_directors")
LINE_KEYS = ("position", "title", "description", "quantity", "unit", "unit_price", "tax_rate", "total")


def date_de(iso: str | None) -> str:
    if not iso:
        return ""
    y, m, d = iso[:10].split("-")
    return f"{d}.{m}.{y}"


def fill_placeholders(text: str, snap: dict) -> str:
    contact = (snap["buyer"].get("contact") or "").split()
    values = {
        "vorname": contact[0] if contact else "",
        "angebotsnummer": snap["number"] if snap["kind"] == "angebot" else (snap.get("source_number") or ""),
        "gueltig_bis": date_de(snap.get("valid_until")),
        "faellig_am": date_de(snap.get("due_date")),
        "betrag": fmt_eur(snap.get("due", 0)),
        "iban": snap["seller"].get("iban", ""),
        # Empfängerüberprüfung der Banken (seit 10/2025): der Name muss zum Kontoinhaber passen
        "kontoinhaber": snap["seller"].get("name", ""),
    }
    return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), text or "")


def build_snapshot(doc: dict, settings: dict, number: str | None, prepaid: list[dict],
                   source_number: str | None) -> dict:
    lines = [{k: l[k] for k in LINE_KEYS} for l in doc["lines"]]
    allowances = [{"label": a["label"], "amount": a["amount"], "tax_rate": a["tax_rate"]} for a in doc["allowances"]]
    totals = compute([Line(l["quantity"], l["unit_price"], l["tax_rate"]) for l in lines],
                     [Allowance(a["amount"], a["tax_rate"]) for a in allowances]).as_dict()
    snap = {
        "format_version": FORMAT_VERSION, "kind": doc["kind"], "number": number,
        **{k: doc.get(k) for k in ("doc_date", "service_date", "service_from", "service_to", "due_date",
                                   "valid_until", "subject", "notes")},
        "conditions": doc.get("conditions", []),
        "seller": {k: settings.get(k, "") for k in SELLER_KEYS},
        "buyer": dict(doc["customer"]),
        "lines": lines, "allowances": allowances, "totals": totals,
        "prepaid": [dict(p) for p in prepaid],
        "due": totals["gross"] - sum(p["gross"] for p in prepaid),
        "source_number": source_number,
        "tax_treatment": doc.get("tax_treatment") or "inland",
        "share_percent": doc.get("share_percent"),
    }
    note = TREATMENT_NOTES.get(snap["tax_treatment"])
    if note:
        snap["tax_note"] = note.format(seller_vat=settings.get("vat_id", ""), buyer_vat=doc["customer"].get("vat_id", ""))
    snap["intro"] = fill_placeholders(doc.get("intro", ""), snap)
    snap["closing"] = fill_placeholders(doc.get("closing", ""), snap)
    if doc.get("payment_note") is not None:
        snap["payment_note"] = fill_placeholders(doc["payment_note"], snap)
    return snap
