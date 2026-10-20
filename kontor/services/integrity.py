"""`kontor pruefen`: Protokollkette, Archiv-Prüfsummen, Schnappschüsse, Lücken in den Nummernkreisen."""
from __future__ import annotations

import hashlib
import json
import re

from ..context import Ctx
from ..store import audit


def check(ctx: Ctx) -> list[str]:
    problems = list(audit.verify(ctx.conn))
    for r in ctx.conn.execute("SELECT id, number, pdf_path, sha256, snapshot_json, net, tax, gross FROM documents "
                              "WHERE status <> 'entwurf' ORDER BY number"):
        totals = json.loads(r["snapshot_json"] or "{}").get("totals", {})
        if (r["net"], r["tax"], r["gross"]) != (totals.get("net"), totals.get("tax"), totals.get("gross")):
            problems.append(f"{r['number']}: Summe in der Datenbank weicht vom Schnappschuss ab")
        pdf = ctx.data / (r["pdf_path"] or "")
        if not r["pdf_path"] or not pdf.is_file():
            problems.append(f"{r['number']}: Archiv-PDF fehlt")
            continue
        if hashlib.sha256(pdf.read_bytes()).hexdigest() != r["sha256"]:
            problems.append(f"{r['number']}: Prüfsumme des Archiv-PDF stimmt nicht")
        js = pdf.with_suffix(".json")
        if not js.is_file() or js.read_text(encoding="utf-8") != r["snapshot_json"]:
            problems.append(f"{r['number']}: Schnappschuss im Archiv weicht von der Datenbank ab")
    for c in ctx.conn.execute("SELECT c.version, c.pdf_path, c.sha256, d.number FROM document_corrections c "
                              "JOIN documents d ON d.id = c.document_id ORDER BY c.id"):
        f = ctx.data / c["pdf_path"]
        if not f.is_file() or hashlib.sha256(f.read_bytes()).hexdigest() != c["sha256"]:
            problems.append(f"{c['number']}: Berichtigung {c['version']} fehlt oder Prüfsumme stimmt nicht")
    for r in ctx.conn.execute("SELECT id, file_path, sha256 FROM receipts ORDER BY id"):
        f = ctx.data / r["file_path"]
        if not f.is_file():
            problems.append(f"Beleg {r['id']}: Datei fehlt ({r['file_path']})")
        elif hashlib.sha256(f.read_bytes()).hexdigest() != r["sha256"]:
            problems.append(f"Beleg {r['id']}: Prüfsumme der Datei stimmt nicht")
    for seq in ctx.conn.execute("SELECT prefix, last, start FROM number_counters"):
        pattern = re.compile(rf"^{re.escape(seq['prefix'])}-\d{{4}}-(\d+)$")
        used = {int(m.group(1)) for (number,) in ctx.conn.execute(
            "SELECT number FROM documents WHERE number LIKE ?", (f"{seq['prefix']}-%",))
            if (m := pattern.match(number))}
        for n in range(seq["start"] + 1, seq["last"] + 1):
            if n not in used:
                problems.append(f"Nummernkreis {seq['prefix']}: laufende Nummer {n:03d} fehlt")
    return problems
