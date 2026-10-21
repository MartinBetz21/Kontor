"""Belege: alles kommt zuerst in eingang/, wird dort vorbefüllt (XML automatisch, Fotos durch Claude)
und vom Menschen bestätigt. Erst dann wandert die Datei nach belege/JJJJ/MM/."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from ..context import Ctx, require_human
from ..core.errors import KontorError
from ..core.tax_codes import TAX_KINDS, complete_amounts
from ..render.zugferd import extract_xml, read_xml
from ..store import audit
from ..store.db import tx
from . import masterdata as md
from .documents import _iso

SUFFIXES = {".pdf", ".jpg", ".jpeg", ".png", ".heic", ".xml"}
ORIGINS = ("upload", "telegram", "cli")
FIELDS = ("receipt_date", "supplier", "receipt_number", "net", "tax", "gross", "tax_kind", "category_id",
          "bewirtung_anlass", "bewirtung_teilnehmer",
          "paid_on", "note")


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w.\-]+", "_", Path(name).name)[-80:] or "beleg"


def _guess_kind(parsed: dict) -> str | None:
    if parsed["tax"] and parsed["tax_rates"] == ["19"]:
        return "inland_19"
    if parsed["tax"] and parsed["tax_rates"] == ["7"]:
        return "inland_7"
    return None


def add_file(ctx: Ctx, content: bytes, original_name: str, origin: str, note: str = "",
             source_ref: str | None = None) -> dict:
    suffix = Path(original_name).suffix.lower()
    if suffix not in SUFFIXES:
        raise KontorError(f"Dateityp {suffix or '?'} wird nicht angenommen (PDF, JPG, PNG, HEIC, XML).")
    if not content:
        raise KontorError("Die Datei ist leer.")
    if origin not in ORIGINS:
        raise KontorError(f"Herkunft {origin!r} gibt es nicht.")
    sha = hashlib.sha256(content).hexdigest()
    row = ctx.conn.execute("SELECT id FROM receipts WHERE sha256 = ?", (sha,)).fetchone()
    if row:
        return {"id": row[0], "duplicate": True, "parsed": False}
    parsed = None
    try:
        xml = content if suffix == ".xml" else (extract_xml(content) if suffix == ".pdf" else None)
        parsed = read_xml(xml) if xml else None
    except Exception as exc:   # eine unlesbare E-Rechnung bleibt eine normale Datei im Eingang
        note = "\n".join(filter(None, [note, f"E-Rechnung nicht lesbar: {exc}"[:300]]))
    target = ctx.data / "eingang" / f"{sha[:12]}-{_safe_name(original_name)}"
    target.write_bytes(content)
    values = {"supplier": "", "receipt_number": "", "receipt_date": None, "net": None, "tax": None, "gross": None,
              "tax_kind": None}
    if parsed:
        values.update({k: parsed[k] for k in ("supplier", "receipt_number", "receipt_date", "net", "tax", "gross")})
        values["tax_kind"] = _guess_kind(parsed)
        try:                                    # das Datum bestimmt später den Ordner belege/JJJJ/MM: nie „../“
            values["receipt_date"] = _iso(values["receipt_date"], "Belegdatum")
        except KontorError:
            values["receipt_date"] = None
    try:
        with tx(ctx.conn):
            cur = ctx.conn.execute(
                "INSERT INTO receipts (status, receipt_date, supplier, receipt_number, net, tax, gross, tax_kind, "
                "file_path, original_name, sha256, origin, parsed_json, note, source_ref) "
                "VALUES ('eingang',?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (values["receipt_date"], values["supplier"], values["receipt_number"], values["net"], values["tax"],
                 values["gross"], values["tax_kind"], str(target.relative_to(ctx.data)), original_name, sha, origin,
                 json.dumps(parsed, ensure_ascii=False) if parsed else None, note, source_ref))
            audit.log(ctx.conn, ctx.actor, "beleg_eingang", "beleg", cur.lastrowid,
                      {"datei": original_name, "sha256": sha, "herkunft": origin})
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {"id": cur.lastrowid, "duplicate": False, "parsed": parsed is not None}


def create_own_receipt(ctx: Ctx, data: dict) -> dict:
    """Eigenbeleg: Ausgabe ohne Rechnung (z. B. vom Privatkonto bezahlt). Ohne Rechnung kein Vorsteuerabzug,
    deshalb nur „ohne Vorsteuer“ oder Reverse Charge (dort entsteht die Steuer ohne Rechnung des Leistenden).
    Landet wie jeder Beleg im Eingang; der Mensch bestätigt."""
    from ..render.pdf import render_own_receipt
    kind = data.get("tax_kind") or "ohne_vorsteuer"
    if kind not in ("ohne_vorsteuer", "rc_eu", "rc_drittland"):
        raise KontorError("Ohne Rechnung gibt es keinen Vorsteuerabzug: Steuerart „ohne Vorsteuer“ oder Reverse Charge "
                          "wählen, oder die Rechnung hochladen.")
    missing = [label for key, label in (("receipt_date", "Datum"), ("supplier", "Empfänger"), ("description", "Wofür"),
                                        ("gross", "Betrag"), ("reason", "Grund für den fehlenden Beleg"))
               if not str(data.get(key) or "").strip()]
    if missing:
        raise KontorError("Für den Eigenbeleg fehlt: " + ", ".join(missing) + ".")
    day = _iso(data["receipt_date"], "Datum")
    paid_on = _iso(data.get("paid_on"), "bezahlt am")
    gross = md.to_cents(data["gross"])
    seller = md.get_settings(ctx)
    pdf = render_own_receipt({**data, "receipt_date": day, "paid_on": paid_on, "gross_cents": gross}, seller,
                             TAX_KINDS[kind], ctx.now_date().isoformat())
    via = str(data.get("paid_via") or "").strip()
    note = f"Eigenbeleg: {data['reason'].strip()}" + (f"; bezahlt über {via}" if via else "")
    added = add_file(ctx, pdf, f"eigenbeleg-{day}-{_safe_name(data['supplier'])}.pdf",
                     "cli" if ctx.actor == "cli" else "upload", note=note)
    fields = {"receipt_date": day, "supplier": data["supplier"], "gross": gross, "tax_kind": kind, "paid_on": paid_on,
              "receipt_number": "Eigenbeleg"}
    if data.get("category_id") not in (None, ""):
        fields["category_id"] = data["category_id"]
    update(ctx, added["id"], fields)
    return get_receipt(ctx, added["id"])


def get_receipt(ctx: Ctx, rid: int) -> dict:
    row = ctx.conn.execute("SELECT * FROM receipts WHERE id = ?", (rid,)).fetchone()
    if row is None:
        raise KontorError(f"Beleg {rid} gibt es nicht.")
    return dict(row)


def list_receipts(ctx: Ctx, status: str | None = None, year: int | None = None) -> list[dict]:
    sql, args = ("SELECT r.*, c.name AS category FROM receipts r LEFT JOIN categories c ON c.id = r.category_id "
                 "WHERE 1=1"), []
    if status:
        sql += " AND r.status = ?"
        args.append(status)
    if year:
        sql += " AND substr(COALESCE(r.receipt_date, r.created_at), 1, 4) = ?"
        args.append(str(year))
    return [dict(r) for r in ctx.conn.execute(sql + " ORDER BY COALESCE(r.receipt_date, r.created_at) DESC, r.id DESC",
                                              args)]


def receipt_file(ctx: Ctx, rid: int) -> Path:
    return ctx.data / get_receipt(ctx, rid)["file_path"]


def _clean(fields: dict) -> dict:
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise KontorError(f"Unbekanntes Belegfeld: {', '.join(sorted(unknown))}")
    out = {}
    for k, v in fields.items():
        if k in ("receipt_date", "paid_on"):
            out[k] = _iso(v, k)
        elif k in ("net", "tax", "gross"):
            out[k] = None if v in (None, "") else md.to_cents(v)
        elif k == "tax_kind":
            if v not in TAX_KINDS:
                raise KontorError(f"Steuerart {v!r} gibt es nicht.")
            out[k] = v
        elif k == "category_id":
            out[k] = int(v) if v not in (None, "") else None
        else:
            out[k] = str(v or "").replace("\r\n", "\n").replace("\r", "\n").strip()   # Browser schicken CRLF
    return out


def _sync_payment(ctx: Ctx, r: dict) -> None:
    ctx.conn.execute("DELETE FROM payments WHERE receipt_id = ?", (r["id"],))
    if r["paid_on"]:
        ctx.conn.execute("INSERT INTO payments (pay_date, amount, kind, receipt_id, note) VALUES (?,?,?,?,?)",
                         (r["paid_on"], -r["gross"], "ausgabe", r["id"], r["supplier"]))


def receipt_parts(ctx: Ctx, rid: int) -> list[dict]:
    return [dict(r) for r in ctx.conn.execute(
        "SELECT tax_kind, net, tax FROM receipt_parts WHERE receipt_id = ? ORDER BY id", (rid,))]


def _complete_parts(raw_parts) -> list[tuple[str, int, int, int]]:
    """Teilsummen [{tax_kind, gross | net [, tax]}] → [(Steuerart, netto, steuer, brutto)]."""
    if not isinstance(raw_parts, list) or not raw_parts:
        raise KontorError("Aufteilung: mindestens eine Teilsumme angeben.")
    out = []
    for i, part in enumerate(raw_parts, 1):
        amounts = {k: (None if part.get(k) in (None, "") else md.to_cents(part[k])) for k in ("net", "tax", "gross")}
        if amounts["net"] is None and amounts["gross"] is None:
            raise KontorError(f"Teilsumme {i}: Betrag fehlt.")
        out.append((part.get("tax_kind"), *complete_amounts(part.get("tax_kind"), amounts["net"], amounts["tax"],
                                                            amounts["gross"])))
    return out


def _write_parts(ctx: Ctx, rid: int, parts: list[tuple[str, int, int, int]]) -> None:
    ctx.conn.execute("DELETE FROM receipt_parts WHERE receipt_id = ?", (rid,))
    for kind, net, tax, _ in parts:
        ctx.conn.execute("INSERT INTO receipt_parts (receipt_id, tax_kind, net, tax) VALUES (?,?,?,?)",
                         (rid, kind, net, tax))
    kinds = {p[0] for p in parts}
    ctx.conn.execute("UPDATE receipts SET net = ?, tax = ?, gross = ?, tax_kind = ? WHERE id = ?",
                     (sum(p[1] for p in parts), sum(p[2] for p in parts), sum(p[3] for p in parts),
                      kinds.pop() if len(kinds) == 1 else None, rid))


def _single_part(r: dict) -> list[tuple[str, int, int, int]]:
    return [(r["tax_kind"], *complete_amounts(r["tax_kind"], r["net"], r["tax"], r["gross"]))]


def _parts_from_rows(rows: list[dict]) -> list[tuple[str, int, int, int]]:
    return [(p["tax_kind"], p["net"], p["tax"], p["net"] if p["tax_kind"].startswith("rc_") else p["net"] + p["tax"])
            for p in rows]


def _file_to_month(ctx: Ctx, r: dict) -> None:
    """Geprüfte Belege liegen unter belege/JJJJ/MM nach Belegdatum; ändert sich das Datum, zieht die Datei mit."""
    source = ctx.data / r["file_path"]
    target = ctx.data / "belege" / r["receipt_date"][:4] / r["receipt_date"][5:7] / source.name
    if source == target:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, target)
    ctx.conn.execute("UPDATE receipts SET file_path = ? WHERE id = ?", (str(target.relative_to(ctx.data)), r["id"]))


def _is_bewirtung(ctx: Ctx, category_id: int | None) -> bool:
    row = ctx.conn.execute("SELECT name FROM categories WHERE id = ?", (category_id,)).fetchone()
    return bool(row) and row["name"] == "Bewirtung"


def update(ctx: Ctx, rid: int, fields: dict) -> None:
    r = get_receipt(ctx, rid)
    if r["status"] == "geprueft":
        require_human(ctx)
    fields = dict(fields)
    raw_parts = fields.pop("parts", None)
    clean = _clean(fields)
    parts = _complete_parts(raw_parts) if raw_parts is not None else None
    if clean.get("category_id") is not None and not ctx.conn.execute(
            "SELECT 1 FROM categories WHERE id = ?", (clean["category_id"],)).fetchone():
        raise KontorError(f"Kategorie {clean['category_id']} gibt es nicht.")
    if not clean and parts is None:
        return
    with tx(ctx.conn):
        if clean:
            ctx.conn.execute(f"UPDATE receipts SET {', '.join(k + ' = ?' for k in clean)} WHERE id = ?",
                             [*clean.values(), rid])
        if parts is not None:
            _write_parts(ctx, rid, parts)
        elif r["status"] == "geprueft" and set(clean) & {"net", "tax", "gross", "tax_kind"}:
            _write_parts(ctx, rid, _single_part(get_receipt(ctx, rid)))
        if r["status"] == "geprueft":
            new = get_receipt(ctx, rid)
            _sync_payment(ctx, new)
            _file_to_month(ctx, new)
        audit.log(ctx.conn, ctx.actor, "beleg_aendern", "beleg", rid,
                  {**clean, **({"parts": raw_parts} if raw_parts is not None else {})})


def confirm(ctx: Ctx, rid: int) -> dict:
    require_human(ctx)
    r = get_receipt(ctx, rid)
    if r["status"] != "eingang":
        raise KontorError("Der Beleg ist schon geprüft.")
    stored = receipt_parts(ctx, rid)
    missing = [label for key, label in (("receipt_date", "Belegdatum"), ("supplier", "Lieferant"),
                                        ("category_id", "Kategorie")) if not r[key]]
    if not stored:
        if not r["tax_kind"]:
            missing.append("Steuerart")
        if r["gross"] is None and r["net"] is None:
            missing.append("Betrag")
    if _is_bewirtung(ctx, r["category_id"]):         # § 4 Abs. 5 Nr. 2 EStG
        if not r["bewirtung_anlass"].strip():
            missing.append("Anlass der Bewirtung")
        if not r["bewirtung_teilnehmer"].strip():
            missing.append("Bewirtete Personen")
    if missing:
        raise KontorError("Zum Bestätigen fehlt: " + ", ".join(missing) + ".")
    parts = _parts_from_rows(stored) if stored else _single_part(r)
    source = ctx.data / r["file_path"]
    folder = ctx.data / "belege" / r["receipt_date"][:4] / r["receipt_date"][5:7]
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / source.name
    os.replace(source, target)
    try:
        with tx(ctx.conn):
            ctx.conn.execute("UPDATE receipts SET status = 'geprueft', file_path = ? WHERE id = ?",
                             (str(target.relative_to(ctx.data)), rid))
            _write_parts(ctx, rid, parts)
            new = get_receipt(ctx, rid)
            _sync_payment(ctx, new)
            audit.log(ctx.conn, ctx.actor, "beleg_bestaetigen", "beleg", rid,
                      {"netto": new["net"], "steuer": new["tax"], "brutto": new["gross"],
                       "teile": [list(p) for p in parts], "sha256": r["sha256"]})
    except BaseException:
        os.replace(target, source)
        raise
    return get_receipt(ctx, rid)


def delete(ctx: Ctx, rid: int) -> None:
    require_human(ctx)
    r = get_receipt(ctx, rid)
    path = ctx.data / r["file_path"]
    with tx(ctx.conn):
        ctx.conn.execute("DELETE FROM payments WHERE receipt_id = ?", (rid,))
        ctx.conn.execute("DELETE FROM receipts WHERE id = ?", (rid,))
        audit.log(ctx.conn, ctx.actor, "beleg_loeschen", "beleg", rid,
                  {"datei": r["original_name"], "sha256": r["sha256"], "status": r["status"]})
    if r["status"] == "geprueft":      # geprüfte Belege verschwinden nie ganz
        keep = ctx.data / "belege" / "geloescht"
        keep.mkdir(parents=True, exist_ok=True)
        os.replace(path, keep / path.name)
    else:
        path.unlink(missing_ok=True)
