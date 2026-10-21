"""Abschließen: Nummer, Schnappschuss, PDF, Archiv, Sperre. Alles oder nichts. Nur der Mensch (Akteur 'ui')."""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path

from ..context import Ctx, require_human
from ..core.checks import finalize_problems, finalize_warnings
from ..core.errors import KontorError
from ..core.snapshot import build_snapshot
from ..core.status import INVOICE_KINDS
from ..render.pdf import render_pdf
from ..store import audit
from ..store.db import tx
from . import documents as docs
from . import masterdata as md


def next_number(conn, prefix: str, year: int, width: int = 3) -> str:
    """Fortlaufend je Kreis über die Jahre; das Jahr im Präfix kommt aus dem Dokumentdatum (Spec 6.2)."""
    conn.execute("INSERT INTO number_counters (prefix) VALUES (?) ON CONFLICT(prefix) DO NOTHING", (prefix,))
    conn.execute("UPDATE number_counters SET last = last + 1 WHERE prefix = ?", (prefix,))
    n = conn.execute("SELECT last FROM number_counters WHERE prefix = ?", (prefix,)).fetchone()[0]
    return f"{prefix}-{year}-{n:0{width}d}"


def logo_path(ctx: Ctx) -> Path | None:
    rel = md.get_settings(ctx)["logo_path"]
    return (ctx.data / rel) if rel else None


def _prepaid(ctx: Ctx, doc: dict) -> list[dict]:
    """Schlussrechnung: abgeschlossene Abschläge zum Angebot. Storno einer Schlussrechnung: deren Abschläge
    mit umgekehrtem Vorzeichen, damit das Storno genau das aufhebt, was die Schlussrechnung berechnet hat."""
    if doc["kind"] == "storno" and doc["source_document_id"]:
        orig = docs.get_document(ctx, doc["source_document_id"])
        prepaid = json.loads(orig["snapshot_json"] or "{}").get("prepaid", [])
        return [{**p, "net": -p["net"], "tax": -p["tax"], "gross": -p["gross"]} for p in prepaid]
    if doc["kind"] != "schluss" or not doc["source_document_id"]:
        return []
    rows = ctx.conn.execute(
        "SELECT id FROM documents WHERE kind = 'abschlag' AND source_document_id = ? "
        "AND status NOT IN ('entwurf', 'storniert') ORDER BY number", (doc["source_document_id"],)).fetchall()
    out = []
    for r in rows:
        a = docs.get_document(ctx, r[0])
        out.append({"number": a["number"], "rate": a["lines"][0]["tax_rate"], "net": a["net"], "tax": a["tax"],
                    "gross": a["gross"]})
    return out


def _source_number(ctx: Ctx, doc: dict) -> str | None:
    if not doc["source_document_id"]:
        return None
    return docs.get_document(ctx, doc["source_document_id"])["number"]


def preview_pdf(ctx: Ctx, doc_id: int) -> bytes:
    doc = docs.get_document(ctx, doc_id)
    if doc["status"] == "entwurf" and doc["customer_id"] and doc["kind"] != "storno":
        doc["customer"] = md.get_customer(ctx, doc["customer_id"])     # Entwurf zeigt die aktuellen Kundendaten
    snap = build_snapshot(doc, md.get_settings(ctx), doc["number"], _prepaid(ctx, doc), _source_number(ctx, doc))
    return render_pdf(snap, logo_path(ctx), draft=doc["status"] == "entwurf")


def archived_pdf(ctx: Ctx, doc_id: int, version: int | None = None) -> Path:
    """Die gültige Fassung: die letzte Berichtigung, sonst das Original. version=0 liefert immer das Original."""
    doc = docs.get_document(ctx, doc_id)
    if not doc["pdf_path"]:
        raise KontorError("Das Dokument ist noch nicht abgeschlossen.")
    if version == 0 or not doc["corrections"]:
        return ctx.data / doc["pdf_path"]
    pick = doc["corrections"][-1] if version is None else next(
        (c for c in doc["corrections"] if c["version"] == version), None)
    if pick is None:
        raise KontorError(f"Fassung {version} gibt es nicht.")
    return ctx.data / pick["pdf_path"]


def correct(ctx: Ctx, doc_id: int, reason: str) -> dict:
    """Rechnungsberichtigung: gleiche Nummer, gleiche Beträge, Verkäuferangaben aus den aktuellen Einstellungen
    (z. B. die nachgelieferte Steuernummer). Das Original und alle Zahlungen bleiben unverändert."""
    require_human(ctx)
    if not (reason or "").strip():
        raise KontorError("Für die Berichtigung einen Grund angeben, z. B. „Steuernummer ergänzt“.")
    doc = docs.get_document(ctx, doc_id)
    if doc["kind"] == "angebot" or doc["status"] == "entwurf":
        raise KontorError("Berichtigen geht nur bei abgeschlossenen Rechnungen.")
    base = json.loads(doc["snapshot_json"])
    if doc["corrections"]:
        last = ctx.conn.execute("SELECT snapshot_json FROM document_corrections WHERE id = ?",
                                (doc["corrections"][-1]["id"],)).fetchone()[0]
        base = json.loads(last)
    settings = md.get_settings(ctx)
    seller = {k: settings.get(k, "") for k in base["seller"]}
    if seller == base["seller"]:
        raise KontorError("Die Verkäuferangaben sind unverändert, es gibt nichts zu berichtigen.")
    version = len(doc["corrections"]) + 1
    snap = {**base, "seller": seller, "correction": {
        "version": version, "reason": reason.strip(), "date": ctx.now_date().isoformat(),
        "original_date": doc["doc_date"]}}
    pdf = render_pdf(snap, logo_path(ctx))
    folder = ctx.data / "archiv" / doc["doc_date"][:4]
    pdf_file = folder / f"{doc['number']}-berichtigung-{version}.pdf"
    json_file = pdf_file.with_suffix(".json")
    if pdf_file.exists():
        raise KontorError(f"Im Archiv liegt schon {pdf_file.name}.")
    snapshot_json = json.dumps(snap, ensure_ascii=False, indent=1)
    sha = hashlib.sha256(pdf).hexdigest()
    written = []
    try:
        with tx(ctx.conn):
            pdf_file.write_bytes(pdf)
            written.append(pdf_file)
            json_file.write_text(snapshot_json, encoding="utf-8")
            written.append(json_file)
            cur = ctx.conn.execute(
                "INSERT INTO document_corrections (document_id, version, reason, snapshot_json, pdf_path, sha256, "
                "created_at) VALUES (?,?,?,?,?,?,?)",
                (doc_id, version, reason.strip(), snapshot_json, str(pdf_file.relative_to(ctx.data)), sha,
                 datetime.now().isoformat(timespec="seconds")))
            audit.log(ctx.conn, ctx.actor, "berichtigen", "dokument", doc_id,
                      {"nummer": doc["number"], "fassung": version, "grund": reason.strip(), "sha256": sha})
    except BaseException:
        for f in written:
            f.unlink(missing_ok=True)
        raise
    for f in written:
        os.chmod(f, 0o444)
    row = ctx.conn.execute("SELECT * FROM document_corrections WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row)


def warnings(ctx: Ctx, doc_id: int) -> list[str]:
    """Hinweise vor dem Abschließen, die es nicht verhindern."""
    doc = docs.get_document(ctx, doc_id)
    out = finalize_warnings(doc, md.get_settings(ctx))
    prefix = "AN" if doc["kind"] == "angebot" else "RE"
    later = ctx.conn.execute(
        "SELECT number FROM documents WHERE number LIKE ? AND doc_date > ? ORDER BY number DESC LIMIT 1",
        (f"{prefix}-{doc['doc_date'][:4]}-%", doc["doc_date"])).fetchone()
    if later:
        out.append(f"Das Datum liegt vor {later[0]}. Die Nummern liefen dann nicht in Datumsfolge; "
                   "besser das heutige Datum nehmen.")
    elif doc["kind"] in ("rechnung", "abschlag", "schluss") and doc["doc_date"] < ctx.now_date().isoformat():
        out.append(f"Das Rechnungsdatum {doc['doc_date']} liegt vor heute, das Zahlungsziel lief dann schon. "
                   "Bei einem früh angelegten Entwurf das heutige Datum nehmen.")
    return out


def finalize(ctx: Ctx, doc_id: int) -> dict:
    require_human(ctx)
    written: list[Path] = []
    try:
        with tx(ctx.conn):     # BEGIN IMMEDIATE vor dem Lesen: kein anderer Schreiber ändert den Entwurf dazwischen
            doc = docs.get_document(ctx, doc_id)
            if doc["status"] != "entwurf":
                raise KontorError("Nur Entwürfe lassen sich abschließen.")
            settings = md.get_settings(ctx)
            if doc["kind"] == "storno":            # das Storno nennt denselben Empfänger wie die Rechnung, die es aufhebt
                customer = json.loads(docs.get_document(ctx, doc["source_document_id"])["snapshot_json"])["buyer"]
            else:
                customer = md.get_customer(ctx, doc["customer_id"]) if doc["customer_id"] else None
            problems = finalize_problems(doc, settings, customer, md.get_profile(ctx))
            if problems:
                raise KontorError("Abschließen nicht möglich: " + "; ".join(problems) + ".")
            doc["customer"] = customer
            prefix = "AN" if doc["kind"] == "angebot" else "RE"
            year = int(doc["doc_date"][:4])
            number = next_number(ctx.conn, prefix, year, int(md.get_profile(ctx)["nummernstellen"]))
            prepaid = _prepaid(ctx, doc)
            snap = build_snapshot(doc, settings, number, prepaid, _source_number(ctx, doc))
            pdf = render_pdf(snap, logo_path(ctx))
            folder = ctx.data / "archiv" / str(year)
            folder.mkdir(parents=True, exist_ok=True)
            pdf_file, json_file = folder / f"{number}.pdf", folder / f"{number}.json"
            if pdf_file.exists() or json_file.exists():
                raise KontorError(f"Im Archiv liegt schon {pdf_file.name}. Bitte `kontor pruefen` laufen lassen.")
            snapshot_json = json.dumps(snap, ensure_ascii=False, indent=1)
            pdf_file.write_bytes(pdf)
            written.append(pdf_file)
            json_file.write_text(snapshot_json, encoding="utf-8")
            written.append(json_file)
            sha = hashlib.sha256(pdf).hexdigest()
            ctx.conn.execute(
                "UPDATE documents SET number = ?, customer_json = ?, snapshot_json = ?, pdf_path = ?, sha256 = ?, "
                "finalized_at = ?, prepaid_gross = ?, prepaid_tax = ?, status = 'abgeschlossen' WHERE id = ?",
                (number, json.dumps(customer, ensure_ascii=False), snapshot_json,
                 str(pdf_file.relative_to(ctx.data)), sha, datetime.now().isoformat(timespec="seconds"),
                 sum(p["gross"] for p in prepaid), sum(p["tax"] for p in prepaid), doc_id))
            audit.log(ctx.conn, ctx.actor, "abschliessen", "dokument", doc_id,
                      {"nummer": number, "brutto": doc["gross"], "sha256": sha})
    except BaseException:
        for f in written:
            f.unlink(missing_ok=True)
        raise
    for f in written:
        os.chmod(f, 0o444)
    return docs.get_document(ctx, doc_id)


def cancel(ctx: Ctx, doc_id: int) -> dict:
    require_human(ctx)
    orig = docs.get_document(ctx, doc_id)
    if orig["kind"] not in INVOICE_KINDS or orig["status"] not in ("abgeschlossen", "teilbezahlt", "bezahlt"):
        raise KontorError("Stornieren geht nur bei abgeschlossenen Rechnungen.")
    if orig["kind"] == "abschlag":
        for r in ctx.conn.execute("SELECT number, snapshot_json FROM documents WHERE kind = 'schluss' "
                                  "AND status NOT IN ('entwurf', 'storniert') AND source_document_id = ?",
                                  (orig["source_document_id"],)):
            if any(p["number"] == orig["number"] for p in json.loads(r["snapshot_json"])["prepaid"]):
                raise KontorError(f"Der Abschlag ist in der Schlussrechnung {r['number']} verrechnet. "
                                  "Erst die Schlussrechnung stornieren.")
    with tx(ctx.conn):
        storno_id = docs.save_draft(ctx, {
            "kind": "storno", "customer_id": orig["customer_id"], "source_document_id": doc_id,
            "tax_treatment": orig["tax_treatment"],
            "doc_date": ctx.now_date().isoformat(), "service_date": orig["service_date"],
            "service_from": orig["service_from"], "service_to": orig["service_to"],
            "subject": f"Storno zu {orig['number']}", "intro": "", "closing": "", "notes": "",
            "lines": [{**{k: l[k] for k in ("title", "description", "quantity", "unit", "tax_rate")},
                       "unit_price": -l["unit_price"]} for l in orig["lines"]],
            "allowances": [{"label": a["label"], "amount": -a["amount"], "tax_rate": a["tax_rate"]}
                           for a in orig["allowances"]],
        }, allow_storno=True)
        result = finalize(ctx, storno_id)
        docs._set_status(ctx, doc_id, "storniert")
    return result
