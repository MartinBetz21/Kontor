"""Entwürfe anlegen und bearbeiten, Status, Angebot → Rechnung. Abschließen steht in finalize.py."""
from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal

from ..context import Ctx
from ..core.calc import Allowance, Line, compute, line_total
from ..core.errors import KontorError
from ..core.money import parse_qty, percent_of
from ..core.status import ALL_KINDS, INVOICE_KINDS, check_transition
from ..core.tax_codes import TAX_TREATMENTS
from ..store import audit
from ..store.db import tx
from . import masterdata as md

TEXT_FIELDS = ("subject", "intro", "notes", "closing")
DATE_FIELDS = ("doc_date", "service_date", "service_from", "service_to", "due_date", "valid_until")


def _iso(value, field: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError as exc:
        raise KontorError(f"{field}: Datum bitte als JJJJ-MM-TT, nicht {value!r}.") from exc


def _clean_line(raw: dict, position: int) -> dict:
    title = str(raw.get("title", "")).strip()
    if not title:
        raise KontorError(f"Position {position}: Bezeichnung fehlt.")
    quantity = format(parse_qty(str(raw.get("quantity", "1"))).normalize(), "f")
    rate = str(raw.get("tax_rate", "19"))
    if rate not in md.TAX_RATES:
        raise KontorError(f"Position {position}: Steuersatz {rate} gibt es nicht.")
    price = md.to_cents(raw.get("unit_price", 0))
    return {"position": position, "title": title, "description": str(raw.get("description", "")).strip(),
            "quantity": quantity, "unit": str(raw.get("unit", "")).strip(), "unit_price": price,
            "tax_rate": rate, "total": line_total(quantity, price)}


def _clean_allowance(raw: dict, position: int) -> dict:
    label = str(raw.get("label", "")).strip()
    if not label:
        raise KontorError(f"Abzug {position}: Bezeichnung fehlt.")
    rate = str(raw.get("tax_rate", "19"))
    if rate not in md.TAX_RATES:
        raise KontorError(f"Abzug {position}: Steuersatz {rate} gibt es nicht.")
    return {"position": position, "label": label, "amount": md.to_cents(raw.get("amount", 0)), "tax_rate": rate}


def get_document(ctx: Ctx, doc_id: int) -> dict:
    row = ctx.conn.execute("SELECT * FROM documents WHERE id = ?", (doc_id,)).fetchone()
    if row is None:
        raise KontorError(f"Dokument {doc_id} gibt es nicht.")
    d = dict(row)
    d["lines"] = [dict(r) for r in ctx.conn.execute(
        "SELECT * FROM document_lines WHERE document_id = ? ORDER BY position", (doc_id,))]
    d["allowances"] = [dict(r) for r in ctx.conn.execute(
        "SELECT * FROM document_allowances WHERE document_id = ? ORDER BY position", (doc_id,))]
    d["corrections"] = [dict(r) for r in ctx.conn.execute(
        "SELECT id, version, reason, pdf_path, sha256, created_at FROM document_corrections WHERE document_id = ? "
        "ORDER BY version", (doc_id,))]
    d["conditions"] = json.loads(d["conditions_json"])
    d["customer"] = json.loads(d["customer_json"])
    d["paid"] = ctx.conn.execute("SELECT COALESCE(SUM(amount), 0) FROM payments WHERE document_id = ?",
                                 (doc_id,)).fetchone()[0]
    d["due"] = d["gross"] - d["prepaid_gross"]
    d["open"] = d["due"] - d["paid"]
    return d


SHARE_LABEL = "Nicht Teil dieses Abschlags"


def _pct_text(pct: Decimal) -> str:
    return format(pct.normalize(), "f").replace(".", ",")


def _share_percent(raw, kind: str) -> str | None:
    """Anteil einer Abschlagsrechnung mit allen Positionen (Spec 6.4), normalisiert wie „50“ oder „33,5“."""
    if raw in (None, ""):
        return None
    if kind != "abschlag":
        raise KontorError("Ein Anteil gilt nur für Abschlagsrechnungen.")
    pct = Decimal(str(raw).replace(",", "."))
    if not 0 < pct <= 100:
        raise KontorError("Abschlag in Prozent zwischen 0 und 100 angeben.")
    return _pct_text(pct)


def _with_share_allowance(lines: list[dict], allowances: list[dict], share: str) -> list[dict]:
    """Der nicht berechnete Rest ist ein Abzug, immer der letzte, und wird bei jedem Speichern neu gerechnet:
    so gehen Summen und E-Rechnung auf, und das PDF zeigt ihn als Anteil (Spec 6.4)."""
    own = [a for a in allowances if not a["label"].startswith(SHARE_LABEL)]
    rates = {l["tax_rate"] for l in lines} | {a["tax_rate"] for a in own}
    if len(rates) != 1:
        raise KontorError("Abschläge gehen nur bei einem einzigen Steuersatz.")
    full = compute([Line(l["quantity"], l["unit_price"], l["tax_rate"]) for l in lines],
                   [Allowance(a["amount"], a["tax_rate"]) for a in own]).net
    pct = Decimal(share.replace(",", "."))
    rest = full - percent_of(full, pct)
    own = [{**a, "position": i + 1} for i, a in enumerate(own)]
    if rest:
        own.append({"position": len(own) + 1, "label": f"{SHARE_LABEL} ({_pct_text(100 - pct)} %)",
                    "amount": -rest, "tax_rate": rates.pop()})
    return own


def save_draft(ctx: Ctx, data: dict, doc_id: int | None = None, *, allow_storno: bool = False) -> int:
    base = get_document(ctx, doc_id) if doc_id else None
    if base and base["status"] != "entwurf":
        raise KontorError("Nur Entwürfe lassen sich bearbeiten.")
    kind = data.get("kind") or (base["kind"] if base else "rechnung")
    if kind not in ALL_KINDS:
        raise KontorError(f"Dokumentart {kind!r} gibt es nicht.")
    if kind == "storno" and not allow_storno:
        raise KontorError("Stornorechnungen entstehen nur über „Stornieren“ an der Rechnung.")
    settings = md.get_settings(ctx)

    def pick(field, default=None):
        if field in data:
            return data[field]
        return base[field] if base else default

    dates = {f: _iso(pick(f), f) for f in DATE_FIELDS}
    dates["doc_date"] = dates["doc_date"] or ctx.now_date().isoformat()
    doc_day = date.fromisoformat(dates["doc_date"])
    if kind != "angebot" and not dates["due_date"]:
        dates["due_date"] = (doc_day + timedelta(days=int(settings["payment_days"]))).isoformat()
    if kind == "angebot" and not dates["valid_until"]:
        dates["valid_until"] = (doc_day + timedelta(days=int(settings["offer_valid_days"]))).isoformat()
    if bool(dates["service_from"]) != bool(dates["service_to"]):
        raise KontorError("Leistungszeitraum braucht Beginn und Ende.")

    texts = {}
    for f in TEXT_FIELDS:
        if f in data:
            texts[f] = str(data[f] or "").replace("\r\n", "\n").replace("\r", "\n")   # Browser schicken CRLF
        elif base:
            texts[f] = base[f]
        elif f in ("intro", "closing"):
            texts[f] = md.default_text(ctx, kind, "einleitung" if f == "intro" else "schluss")
        else:
            texts[f] = ""
    conditions = data.get("conditions", base["conditions"] if base else [])
    if not all(isinstance(c, (list, tuple)) and len(c) == 2 for c in conditions):
        raise KontorError("Konditionen sind Paare [Bezeichnung, Text].")

    if data.get("new_customer"):              # Kunde direkt aus dem Angebot anlegen
        data = {**data, "customer_id": md.create_customer(ctx, data["new_customer"])}
    customer_id = pick("customer_id")
    customer = md.get_customer(ctx, int(customer_id)) if customer_id else {}
    lines = ([_clean_line(l, i + 1) for i, l in enumerate(data["lines"])] if "lines" in data
             else [{k: l[k] for k in ("position", "title", "description", "quantity", "unit", "unit_price",
                                      "tax_rate", "total")} for l in (base["lines"] if base else [])])
    allowances = ([_clean_allowance(a, i + 1) for i, a in enumerate(data["allowances"])] if "allowances" in data
                  else [{k: a[k] for k in ("position", "label", "amount", "tax_rate")}
                        for a in (base["allowances"] if base else [])])
    share = _share_percent(pick("share_percent"), kind)
    if share is not None:
        allowances = _with_share_allowance(lines, allowances, share)
    totals = compute([Line(l["quantity"], l["unit_price"], l["tax_rate"]) for l in lines],
                     [Allowance(a["amount"], a["tax_rate"]) for a in allowances])
    source = pick("source_document_id")
    payment_mode = pick("payment_mode") or None
    if kind in ("rechnung", "abschlag", "schluss"):
        if "payment_note" in data:
            payment_note = str(data["payment_note"] or "").replace("\r\n", "\n").strip()
        elif base and base.get("payment_note") is not None:
            payment_note = base["payment_note"]
        else:
            payment_note = settings["payment_note_template"]
    else:
        payment_note = None
    if payment_mode not in (None, "vorab", "danach", "50_50"):
        raise KontorError("Zahlungsmodus ist „vorab“, „danach“ oder „50_50“.")

    treatment = pick("tax_treatment") or "inland"
    if treatment not in TAX_TREATMENTS:
        raise KontorError(f"Steuerart {treatment!r} gibt es nicht ({', '.join(TAX_TREATMENTS)}).")
    if treatment != "inland":           # nicht steuerbar bzw. Reverse Charge: keine deutsche Steuer (Spec 18.4)
        lines = [{**l, "tax_rate": "0"} for l in lines]
        allowances = [{**a, "tax_rate": "0"} for a in allowances]
        totals = compute([Line(l["quantity"], l["unit_price"], "0") for l in lines],
                         [Allowance(a["amount"], "0") for a in allowances])
    values = {"kind": kind, "customer_id": customer_id, "tax_treatment": treatment, "customer_json": json.dumps(customer, ensure_ascii=False),
              **dates, **texts, "conditions_json": json.dumps([list(c) for c in conditions], ensure_ascii=False),
              "source_document_id": source, "payment_mode": payment_mode, "payment_note": payment_note,
              "share_percent": share,
              "net": totals.net, "tax": totals.tax, "gross": totals.gross}
    with tx(ctx.conn):
        if base:
            ctx.conn.execute(
                f"UPDATE documents SET {', '.join(k + ' = ?' for k in values)}, "
                "updated_at = strftime('%Y-%m-%dT%H:%M:%S','now','localtime') WHERE id = ?",
                [*values.values(), doc_id])
            ctx.conn.execute("DELETE FROM document_lines WHERE document_id = ?", (doc_id,))
            ctx.conn.execute("DELETE FROM document_allowances WHERE document_id = ?", (doc_id,))
        else:
            cur = ctx.conn.execute(f"INSERT INTO documents ({', '.join(values)}) VALUES ({', '.join('?' * len(values))})",
                                   list(values.values()))
            doc_id = cur.lastrowid
        for l in lines:
            ctx.conn.execute("INSERT INTO document_lines (document_id, position, title, description, quantity, unit, "
                             "unit_price, tax_rate, total) VALUES (?,?,?,?,?,?,?,?,?)",
                             (doc_id, l["position"], l["title"], l["description"], l["quantity"], l["unit"],
                              l["unit_price"], l["tax_rate"], l["total"]))
        for a in allowances:
            ctx.conn.execute("INSERT INTO document_allowances (document_id, position, label, amount, tax_rate) "
                             "VALUES (?,?,?,?,?)", (doc_id, a["position"], a["label"], a["amount"], a["tax_rate"]))
        audit.log(ctx.conn, ctx.actor, "entwurf_speichern", "dokument", doc_id,
                  {"kind": kind, "net": totals.net, "gross": totals.gross, "lines": len(lines)})
    return doc_id


def list_documents(ctx: Ctx, group: str | None = None, status: str | None = None,
                   year: int | None = None) -> list[dict]:
    sql, args = "SELECT id FROM documents WHERE 1=1", []
    if group == "angebote":
        sql += " AND kind = 'angebot'"
    elif group == "rechnungen":
        sql += " AND kind <> 'angebot'"
    if status:
        sql += " AND status = ?"
        args.append(status)
    if year:
        sql += " AND substr(doc_date, 1, 4) = ?"
        args.append(str(year))
    ids = [r[0] for r in ctx.conn.execute(sql + " ORDER BY doc_date DESC, id DESC", args)]
    return [get_document(ctx, i) for i in ids]


def delete_draft(ctx: Ctx, doc_id: int) -> None:
    d = get_document(ctx, doc_id)
    if d["status"] != "entwurf":
        raise KontorError("Nur Entwürfe lassen sich löschen.")
    with tx(ctx.conn):
        ctx.conn.execute("DELETE FROM documents WHERE id = ?", (doc_id,))
        audit.log(ctx.conn, ctx.actor, "entwurf_loeschen", "dokument", doc_id, {"kind": d["kind"], "gross": d["gross"]})


def set_status(ctx: Ctx, doc_id: int, new: str) -> None:
    """Von außen nur für Angebote (angenommen/abgelehnt). Rechnungen ändern ihren Status ausschließlich über
    Zahlungen (payments.py) und Stornieren (finalize.cancel)."""
    if get_document(ctx, doc_id)["kind"] != "angebot":
        raise KontorError("Der Status einer Rechnung ändert sich nur über Zahlungen und „Stornieren“.")
    _set_status(ctx, doc_id, new)


def _set_status(ctx: Ctx, doc_id: int, new: str) -> None:
    d = get_document(ctx, doc_id)
    if d["status"] == new:
        return
    check_transition(d["kind"], d["status"], new)
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE documents SET status = ? WHERE id = ?", (new, doc_id))
        audit.log(ctx.conn, ctx.actor, "status", "dokument", doc_id, {"von": d["status"], "nach": new})


def _content(d: dict) -> dict:
    return {"customer_id": d["customer_id"], "subject": d["subject"], "notes": d["notes"],
            "tax_treatment": d["tax_treatment"], "share_percent": d.get("share_percent"),
            "lines": [{k: l[k] for k in ("title", "description", "quantity", "unit", "unit_price", "tax_rate")}
                      for l in d["lines"]],
            "allowances": [{k: a[k] for k in ("label", "amount", "tax_rate")} for a in d["allowances"]]}


def invoice_from_offer(ctx: Ctx, offer_id: int, mode: str = "voll", percent: str | None = None) -> int:
    o = get_document(ctx, offer_id)
    if o["kind"] != "angebot" or o["status"] not in ("abgeschlossen", "angenommen"):
        raise KontorError("Rechnungen entstehen aus abgeschlossenen oder angenommenen Angeboten.")
    base = {**_content(o), "source_document_id": offer_id}
    if mode == "voll":
        return save_draft(ctx, {**base, "kind": "rechnung", "subject": o["subject"] or f"Rechnung zu {o['number']}"})
    base["notes"] = ""          # Hinweise des Angebots (Retainer, Lizenzen) gehören nicht auf Teilrechnungen
    if mode == "schluss":
        return _schluss_draft(ctx, o, base)
    if mode == "abschlag":
        share = _share_percent(percent, "abschlag")
        if share is None:
            raise KontorError("Abschlag in Prozent zwischen 0 und 100 angeben.")
        new = save_draft(ctx, {**base, "kind": "abschlag", "share_percent": share,
                               "subject": _subject(f"Abschlag {share} % auf Angebot {o['number']}", o)})
        if share != "100":
            _schluss_draft(ctx, o, base)     # Spec 6.4: die Schlussrechnung liegt gleich als Entwurf bereit
        return new
    raise KontorError(f"Unbekannte Art: {mode} (voll, abschlag, schluss).")


def _schluss_draft(ctx: Ctx, o: dict, base: dict) -> int:
    """Gibt es schon einen Entwurf, wird er geöffnet; eine abgeschlossene Schlussrechnung gibt es nur einmal."""
    other = ctx.conn.execute("SELECT id, number, status FROM documents WHERE kind = 'schluss' AND source_document_id = ? "
                             "AND status <> 'storniert'", (o["id"],)).fetchone()
    if other and other["status"] == "entwurf":
        return other["id"]
    if other:
        raise KontorError(f"Zu diesem Angebot gibt es schon die Schlussrechnung {other['number']}.")
    return save_draft(ctx, {**base, "kind": "schluss", "share_percent": None,
                            "subject": _subject(f"Schlussrechnung zu Angebot {o['number']}", o)})


def _subject(head: str, o: dict) -> str:
    return f"{head} · {o['subject']}" if o["subject"] else head


def copy_document(ctx: Ctx, doc_id: int) -> int:
    d = get_document(ctx, doc_id)
    kind = "rechnung" if d["kind"] == "storno" else d["kind"]
    return save_draft(ctx, {**_content(d), "kind": kind, "intro": d["intro"], "closing": d["closing"],
                            "conditions": d["conditions"]})


def save_line_as_catalog(ctx: Ctx, doc_id: int, position: int) -> int:
    d = get_document(ctx, doc_id)
    line = next((l for l in d["lines"] if l["position"] == position), None)
    if line is None:
        raise KontorError(f"Position {position} gibt es in diesem Dokument nicht.")
    return md.create_catalog_item(ctx, {k: line[k] for k in ("title", "description", "unit", "unit_price", "tax_rate")})
