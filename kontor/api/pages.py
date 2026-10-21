"""Seiten. Formulare posten auf dieselbe Adresse und leiten danach um (303), Fehler zeigt error.html."""
from __future__ import annotations

import asyncio
import io
import shutil
import subprocess
from datetime import date
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.datastructures import UploadFile

from ..core.calc import Allowance, Line, compute
from ..core.errors import KontorError
from ..core.tax_codes import period_of
from ..importers.telegram import SshTransport, pull
from ..reports.abschluss import auffaelligkeiten, bilanz_guv
from ..reports.euer import euer
from ..reports.export import journal_csv
from ..reports.overview import overview
from ..reports.ustva import jahresuebersicht, ustva
from ..services import documents as docs
from ..services import finalize as fin
from ..services import integrity
from ..services import journal as jr
from ..services import masterdata as md
from ..services import payments as pay
from ..services import periods as per
from ..services import pipeline
from ..services import receipts as rc
from ..services import series
from ..services import subscriptions as subs
from .app import PREFIX, render

router = APIRouter()


def C(request: Request):
    return request.state.ctx


def go(url: str, hinweis: str = "") -> RedirectResponse:
    if url.startswith("/"):
        url = PREFIX.get() + url
    if hinweis:
        url += ("&" if "?" in url else "?") + "hinweis=" + quote(hinweis)
    return RedirectResponse(url, status_code=303)


def check_confirm(request: Request, form) -> None:
    if form.get("confirm") != request.app.state.confirm[request.state.mandant.id]:
        raise HTTPException(403, "Bestätigung fehlt oder ist veraltet. Bitte die Seite neu laden.")


def _housekeeping(ctx) -> None:
    """Beim Öffnen: abgelaufene Angebote ablehnen (mit Nachfass-Aufgabe), fällige Serienrechnungen als Entwurf anlegen."""
    pipeline.expire_offers(ctx)
    series.generate_due(ctx)


@router.get("/")
async def home(request: Request):
    ctx = C(request)
    _housekeeping(ctx)
    ergebnis = None
    if md.get_profile(ctx)["gewinnermittlung"] == "bilanz":
        try:
            ergebnis = bilanz_guv(ctx, ctx.now_date().year)["positionen"]["erg"]
        except KontorError:
            pass
    return render(request, "overview.html", {"nav": "home", "o": overview(ctx), "ergebnis": ergebnis})


@router.get("/journal")
async def journal_page(request: Request, jahr: int | None = None):
    ctx = C(request)
    year = jahr or ctx.now_date().year
    try:
        report, problem = bilanz_guv(ctx, year), ""
    except KontorError as exc:
        report, problem = None, str(exc)
    locked = ctx.conn.execute("SELECT note, locked_at FROM journal_locks WHERE year = ?", (year,)).fetchone()
    manual = [r for r in jr.lines(ctx, year) if r["art"] == "manuell"]
    return render(request, "journal.html", {"nav": "journal", "year": year, "r": report, "problem": problem,
                                            "findings": auffaelligkeiten(ctx, year), "manual": manual,
                                            "locked": locked, "names": jr.account_names(ctx),
                                            "counts": dict(ctx.conn.execute("SELECT art, COUNT(*) FROM journal "
                                                                            "WHERE year = ? GROUP BY art", (year,)))})


@router.get("/bank")
async def bank_page(request: Request, jahr: int | None = None, offen: int = 1):
    from ..services import bank
    ctx = C(request)
    year = jahr or ctx.now_date().year
    txs = bank.transactions(ctx, year, offen=bool(offen))
    for t in txs:
        t["vorschlag"] = bank.suggest(ctx, t) if t["kind"] is None and not t["duplicate"] else None
    invoices = [dict(r) for r in ctx.conn.execute(
        "SELECT id, number, gross, customer_json FROM documents WHERE kind <> 'angebot' AND status IN "
        "('abgeschlossen', 'teilbezahlt') ORDER BY number")]
    return render(request, "bank.html", {"nav": "bank", "year": year, "offen": offen, "txs": txs,
                                         "invoices": invoices, "accounts": bank.accounts(ctx),
                                         "anchors": bank.anchors(ctx),
                                         "open_count": len(bank.transactions(ctx, year, offen=True))})


@router.post("/bank/{tid}/zuordnen")
async def bank_assign(request: Request, tid: int):
    from ..services import bank
    f = await request.form()
    jahr = f.get("jahr", "")
    bank.assign(C(request), tid, f.get("art", ""), document_id=int(f["rechnung"]) if f.get("rechnung") else None,
                account=f.get("konto", "").strip(), tax_key=f.get("steuer", ""), note=f.get("notiz", ""))
    return go(f"/bank?jahr={jahr}", "Zugeordnet")


@router.post("/bank/{tid}/loesen")
async def bank_unassign(request: Request, tid: int):
    from ..services import bank
    f = await request.form()
    bank.unassign(C(request), tid, f.get("grund", "") or "in der Oberfläche gelöst")
    return go(f"/bank?jahr={f.get('jahr', '')}&offen=0", "Zuordnung gelöst")


@router.post("/bank/vorschlaege")
async def bank_apply(request: Request):
    from ..services import bank
    f = await request.form()
    n = bank.apply_suggestions(C(request), int(f.get("jahr", "0")))
    return go(f"/bank?jahr={f.get('jahr', '')}", f"{n} Vorschläge übernommen")


@router.get("/journal/konto/{konto}")
async def journal_account(request: Request, konto: str, jahr: int | None = None):
    ctx = C(request)
    year = jahr or ctx.now_date().year
    rows = jr.lines(ctx, year, konto)
    running, out = 0, []
    for r in rows:
        running += r["amount"]
        out.append({**r, "saldo": running})
    return render(request, "kontenblatt.html", {"nav": "journal", "year": year, "konto": konto,
                                                "name": jr.account_names(ctx).get(konto, ""), "rows": out})


@router.post("/journal/sperren")
async def journal_lock(request: Request):
    f = await request.form()
    check_confirm(request, f)
    year = int(f.get("jahr", "0"))
    jr.lock_year(C(request), year, f.get("vermerk", ""))
    return go(f"/journal?jahr={year}", f"{year} gesperrt")


@router.get("/angebote")
async def offers_board(request: Request):
    _housekeeping(C(request))
    return render(request, "pipeline.html", {"nav": "angebote", "b": pipeline.board(C(request)),
                                             "modes": pipeline.PAYMENT_MODES})


@router.post("/aufgaben/{fid}/erledigt")
async def followup_done(request: Request, fid: int):
    f = await request.form()
    pipeline.close_followup(C(request), fid, f.get("notiz", ""))
    return go("/", "Aufgabe erledigt")


@router.post("/positionen/reihenfolge")
async def catalog_reorder(request: Request):
    f = await request.form()
    md.reorder_catalog(C(request), [i for i in str(f.get("ids", "")).split(",") if i.strip()])
    return JSONResponse({"ok": True})


@router.get("/abos")
async def subscriptions_page(request: Request):
    return render(request, "subscriptions.html", {"nav": "abos", "o": subs.overview(C(request)),
                                                  "intervals": subs.INTERVALS})


@router.post("/abos")
async def subscription_create(request: Request):
    f = await request.form()
    subs.create(C(request), {k: f.get(k, "") for k in subs.FIELDS})
    return go("/abos", "Abo angelegt")


@router.post("/abos/{sid}")
async def subscription_update(request: Request, sid: int):
    f = await request.form()
    subs.update(C(request), sid, {k: f.get(k, "") for k in subs.FIELDS})
    return go("/abos", "Gespeichert")


@router.get("/serien")
async def series_list(request: Request, von: int | None = None):
    ctx = C(request)
    source = docs.get_document(ctx, von) if von else None
    return render(request, "series.html", {"nav": "serien", "items": series.list_all(ctx),
                                           "customers": md.list_customers(ctx), "source": source,
                                           "catalog": md.list_catalog(ctx)})


@router.post("/serien")
async def series_create(request: Request):
    f = await request.form()
    series.create(C(request), {
        "customer_id": f.get("customer_id"), "subject": f.get("subject", ""), "start_date": f.get("start_date", ""),
        "count": f.get("count", ""), "source_document_id": f.get("source_document_id") or None,
        "lines": [{"title": f.get("line_title", ""), "description": f.get("line_description", ""), "quantity": "1",
                   "unit_price": f.get("line_price", "") or "0", "tax_rate": f.get("line_tax", "19")}]})
    return go("/serien", "Serie angelegt; fällige Monate erscheinen als Rechnungsentwurf")


@router.post("/serien/{sid}/aktiv")
async def series_toggle(request: Request, sid: int):
    f = await request.form()
    series.set_active(C(request), sid, f.get("aktiv") == "1")
    return go("/serien", "Gespeichert")


@router.get("/api/boot")
async def api_boot(request: Request):
    """Kennung dieses Serverstarts; die Seite lädt neu, wenn sie sich ändert (Code wurde aktualisiert)."""
    return JSONResponse({"boot": request.app.state.boot})


@router.get("/api/uebersicht")
async def api_overview(request: Request):
    o = overview(C(request))
    for d in o["open_invoices"] + o["drafts"]:
        d.pop("snapshot_json", None)
    return JSONResponse(o)


# -- Kunden -------------------------------------------------------------------
@router.get("/kunden")
async def customers(request: Request):
    return render(request, "customers.html", {"nav": "kunden", "items": md.list_customers(C(request), True)})


@router.post("/kunden")
async def customer_create(request: Request):
    f = await request.form()
    cid = md.create_customer(C(request), {k: f.get(k, "") for k in md.CUSTOMER_FIELDS})
    return go(f"/kunden/{cid}", "Kunde angelegt")


@router.get("/kunden/{cid}")
async def customer_show(request: Request, cid: int):
    ctx = C(request)
    items = [d for d in docs.list_documents(ctx) if d["customer_id"] == cid]
    return render(request, "customer.html", {"nav": "kunden", "c": md.get_customer(ctx, cid), "docs": items})


@router.post("/kunden/{cid}")
async def customer_update(request: Request, cid: int):
    f = await request.form()
    data = {k: f.get(k, "") for k in md.CUSTOMER_FIELDS}
    data["archived"] = bool(f.get("archived"))
    md.update_customer(C(request), cid, data)
    return go(f"/kunden/{cid}", "Gespeichert")


# -- Positionen & Texte -------------------------------------------------------
@router.get("/positionen")
async def catalog(request: Request):
    ctx = C(request)
    return render(request, "catalog.html", {"nav": "positionen", "items": md.list_catalog(ctx, include_inactive=True),
                                            "templates_": md.list_templates(ctx)})


@router.post("/positionen")
async def catalog_create(request: Request):
    f = await request.form()
    md.create_catalog_item(C(request), {k: f.get(k, "") for k in ("title", "description", "unit", "unit_price",
                                                                  "tax_rate")})
    return go("/positionen", "Position angelegt")


@router.post("/positionen/{iid}")
async def catalog_update(request: Request, iid: int):
    f = await request.form()
    data = {k: f.get(k, "") for k in ("title", "description", "unit", "unit_price", "tax_rate")}
    data["active"] = bool(f.get("active"))
    md.update_catalog_item(C(request), iid, data)
    return go("/positionen", "Gespeichert")


@router.post("/texte")
async def template_create(request: Request):
    f = await request.form()
    md.create_template(C(request), {"kind": f.get("kind"), "slot": f.get("slot"), "title": f.get("title", ""),
                                    "body": f.get("body", ""), "is_default": bool(f.get("is_default"))})
    return go("/positionen", "Textvorlage angelegt")


@router.post("/texte/{tid}")
async def template_update(request: Request, tid: int):
    f = await request.form()
    md.update_template(C(request), tid, {"title": f.get("title", ""), "body": f.get("body", ""),
                                         "is_default": bool(f.get("is_default"))})
    return go("/positionen", "Gespeichert")


# -- Einstellungen, Protokoll, Prüfung ---------------------------------------
@router.get("/einstellungen")
async def settings(request: Request):
    return render(request, "settings.html", {"nav": "einstellungen", "s": md.get_settings(C(request))})


@router.post("/einstellungen")
async def settings_save(request: Request):
    ctx = C(request)
    f = await request.form()
    values = {k: f.get(k, "") for k in md.SETTINGS_DEFAULTS if k != "logo_path" and k in f}
    values["tax_number_pending"] = "1" if f.get("tax_number_pending") else ""      # Checkbox fehlt, wenn leer
    logo = f.get("logo")
    if isinstance(logo, UploadFile) and logo.filename:
        values["logo_path"] = md.set_logo(ctx, await logo.read(), logo.filename)
    md.set_settings(ctx, values)
    return go("/einstellungen", "Gespeichert")


@router.get("/protokoll")
async def audit_log(request: Request):
    rows = [dict(r) for r in C(request).conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 300")]
    return render(request, "audit.html", {"nav": "einstellungen", "rows": rows})


@router.get("/pruefen")
async def check(request: Request):
    return render(request, "check.html", {"nav": "einstellungen", "problems": integrity.check(C(request))})


# -- Angebote und Rechnungen --------------------------------------------------
EDITOR_FIELDS = ("kind", "doc_date", "service_date", "service_from", "service_to", "due_date", "valid_until",
                 "subject", "intro", "notes", "closing", "payment_note", "tax_treatment")


def parse_editor(f) -> dict:
    data = {k: f.get(k, "") for k in EDITOR_FIELDS if k in f}
    data["customer_id"] = int(f["customer_id"]) if f.get("customer_id") else None
    if f.get("service_mode") == "zeitraum":
        data["service_date"] = ""
    else:
        data["service_from"] = data["service_to"] = ""
    data["lines"] = [
        {"title": t, "description": d, "quantity": q.strip() or "1", "unit": "", "unit_price": p.strip() or "0",
         "tax_rate": r}
        for t, d, q, p, r in zip(f.getlist("line_title"), f.getlist("line_description"), f.getlist("line_quantity"),
                                 f.getlist("line_price"), f.getlist("line_tax"))
        if t.strip() or p.strip()]
    data["allowances"] = [{"label": l, "amount": a.strip() or "0", "tax_rate": r}
                          for l, a, r in zip(f.getlist("al_label"), f.getlist("al_amount"), f.getlist("al_tax"))
                          if l.strip() or a.strip()]
    data["conditions"] = [[k, v] for k, v in zip(f.getlist("cond_key"), f.getlist("cond_value")) if k.strip()]
    if "payment_mode" in f:
        data["payment_mode"] = f.get("payment_mode") or None
    if not data["customer_id"] and (f.get("new_company", "").strip() or f.get("new_contact", "").strip()):
        data["new_customer"] = {k: f.get(f"new_{k}", "") for k in ("company", "contact", "street", "postcode", "city",
                                                                    "email")}
    return data


def _nav(kind: str) -> str:
    return "angebote" if kind == "angebot" else "rechnungen"


def _new_doc(ctx, art: str, kunde: int | None) -> dict:
    return {"id": None, "payment_mode": None, "payment_note": None, "tax_treatment": "inland", "kind": art, "status": "entwurf", "number": None, "customer_id": kunde,
            "doc_date": ctx.now_date().isoformat(), "service_date": "", "service_from": "", "service_to": "",
            "due_date": "", "valid_until": "", "subject": "", "notes": "",
            "intro": md.default_text(ctx, art, "einleitung"), "closing": md.default_text(ctx, art, "schluss"),
            "lines": [], "allowances": [], "conditions": [], "net": 0, "tax": 0, "gross": 0}


def _editor(request: Request, d: dict):
    ctx = C(request)
    totals = compute([Line(l["quantity"], l["unit_price"], l["tax_rate"]) for l in d["lines"]],
                     [Allowance(a["amount"], a["tax_rate"]) for a in d["allowances"]]).as_dict()
    warnings = fin.warnings(ctx, d["id"]) if d.get("id") else []
    return render(request, "editor.html", {"nav": _nav(d["kind"]), "d": d, "t": totals, "warnings": warnings,
                                           "customers": md.list_customers(ctx), "catalog": md.list_catalog(ctx),
                                           "modes": pipeline.PAYMENT_MODES, "settings": md.get_settings(ctx)})


@router.get("/dokumente")
async def doc_list(request: Request, gruppe: str = "rechnungen", status: str = "", jahr: str = ""):
    items = docs.list_documents(C(request), gruppe, status or None, int(jahr) if jahr.isdigit() else None)
    return render(request, "documents.html", {"nav": gruppe, "items": items, "gruppe": gruppe, "status": status,
                                              "jahr": jahr})


@router.get("/api/dokumente")
async def api_docs(request: Request):
    items = docs.list_documents(C(request))
    for d in items:
        d.pop("snapshot_json", None)
    return JSONResponse(items)


@router.get("/dokumente/neu")
async def doc_new(request: Request, art: str = "rechnung", kunde: int | None = None):
    if art not in ("angebot", "rechnung"):
        raise KontorError("Neu anlegen geht für Angebote und Rechnungen.")
    return _editor(request, _new_doc(C(request), art, kunde))


@router.post("/dokumente/summen")
async def doc_totals(request: Request):
    f = await request.form()
    try:
        data = parse_editor(f)
        lines = [docs._clean_line(l, i + 1) for i, l in enumerate(data["lines"])]
        allowances = [docs._clean_allowance(a, i + 1) for i, a in enumerate(data["allowances"])]
        t = compute([Line(l["quantity"], l["unit_price"], l["tax_rate"]) for l in lines],
                    [Allowance(a["amount"], a["tax_rate"]) for a in allowances]).as_dict()
        return render(request, "_totals.html", {"t": t})
    except KontorError as exc:
        return render(request, "_totals.html", {"t": None, "message": str(exc)})


@router.get("/katalog/suche")
async def catalog_search(request: Request, q: str = ""):
    return render(request, "_catalog_results.html", {"items": md.list_catalog(C(request), q)[:12]})


@router.post("/dokumente")
async def doc_create(request: Request):
    did = docs.save_draft(C(request), parse_editor(await request.form()))
    return go(f"/dokumente/{did}", "Entwurf gespeichert")


@router.get("/dokumente/{did}")
async def doc_show(request: Request, did: int):
    ctx = C(request)
    d = docs.get_document(ctx, did)
    if d["status"] == "entwurf":
        return _editor(request, d)
    related = [x for x in docs.list_documents(ctx) if x["source_document_id"] == did]
    source = docs.get_document(ctx, d["source_document_id"]) if d["source_document_id"] else None
    return render(request, "document.html", {"nav": _nav(d["kind"]), "d": d, "related": related, "source": source,
                                             "payments": pay.list_payments(ctx, did),
                                             "today": ctx.now_date().isoformat()})


@router.post("/dokumente/{did}")
async def doc_save(request: Request, did: int):
    docs.save_draft(C(request), parse_editor(await request.form()), did)
    return go(f"/dokumente/{did}", "Gespeichert")


@router.post("/dokumente/{did}/loeschen")
async def doc_delete(request: Request, did: int):
    kind = docs.get_document(C(request), did)["kind"]
    docs.delete_draft(C(request), did)
    return go(f"/dokumente?gruppe={_nav(kind)}", "Entwurf gelöscht")


@router.get("/dokumente/{did}/pdf")
async def doc_pdf(request: Request, did: int, download: int = 0, version: int | None = None):
    ctx = C(request)
    d = docs.get_document(ctx, did)
    if d["status"] == "entwurf":
        body, name = fin.preview_pdf(ctx, did), f"Entwurf-{did}.pdf"
    else:
        path = fin.archived_pdf(ctx, did, version)
        body, name = path.read_bytes(), path.name
    disposition = "attachment" if download else "inline"
    return Response(body, media_type="application/pdf",
                    headers={"Content-Disposition": f'{disposition}; filename="{name}"'})


@router.post("/ordner-waehlen")
async def choose_folder(request: Request):
    """Öffnet den Finder-Dialog zur Ordnerwahl und gibt den gewählten Pfad zurück (None = abgebrochen)."""
    f = await request.form()
    path = await asyncio.to_thread(request.app.state.folder_chooser, str(f.get("start") or ""))
    return JSONResponse({"path": path})


@router.post("/dokumente/{did}/speichern")
async def doc_save_to_folder(request: Request, did: int):
    """Fertiges PDF in einen Ordner legen: gewählter Ordner, sonst der Ordner des Kunden, sonst Downloads.
    Der gewählte Ordner wird beim Kunden gemerkt."""
    ctx = C(request)
    f = await request.form()
    d = docs.get_document(ctx, did)
    if d["status"] == "entwurf":
        raise KontorError("Entwürfe lassen sich nicht speichern, erst abschließen. Für einen Blick: Vorschau.")
    customer = md.get_customer(ctx, d["customer_id"]) if d["customer_id"] else {}
    chosen = str(f.get("ziel") or "").strip()
    target_dir = Path(chosen or customer.get("folder") or request.app.state.downloads).expanduser()
    if not (chosen or customer.get("folder")):
        target_dir.mkdir(parents=True, exist_ok=True)          # Standard: Downloads
    if not target_dir.is_dir():
        raise KontorError(f"Den Ordner {target_dir} gibt es nicht.")
    source = fin.archived_pdf(ctx, did)
    target = target_dir / source.name.replace("-berichtigung-", "-berichtigt-")
    shutil.copyfile(source, target)
    if chosen and customer and customer.get("folder") != str(target_dir):
        md.update_customer(ctx, customer["id"], {"folder": str(target_dir)})
    return go(request.headers.get("referer", f"/dokumente/{did}").split("?")[0], f"Gespeichert: {target}")


@router.post("/dokumente/{did}/abschliessen")
async def doc_finalize(request: Request, did: int):
    ctx = C(request)
    f = await request.form()
    check_confirm(request, f)
    if "line_title" in f:                      # aus dem Editor: erst den sichtbaren Stand speichern
        docs.save_draft(ctx, parse_editor(f), did)
    fin.finalize(ctx, did)
    return go(f"/dokumente/{did}", "Abgeschlossen. PDF herunterladen und verschicken.")


@router.post("/dokumente/{did}/stornieren")
async def doc_cancel(request: Request, did: int):
    check_confirm(request, await request.form())
    storno = fin.cancel(C(request), did)
    return go(f"/dokumente/{storno['id']}", "Storniert")


@router.post("/dokumente/{did}/berichtigen")
async def doc_correct(request: Request, did: int):
    f = await request.form()
    check_confirm(request, f)
    c = fin.correct(C(request), did, f.get("grund", ""))
    return go(f"/dokumente/{did}", f"Berichtigte Fassung {c['version']} erzeugt")


@router.post("/dokumente/{did}/zahlung")
async def doc_payment(request: Request, did: int):
    f = await request.form()
    pay.record_payment(C(request), did, f.get("datum", ""), f.get("betrag", ""), f.get("notiz", ""))
    return go(f"/dokumente/{did}", "Zahlung erfasst")


@router.post("/zahlungen/{pid}/loeschen")
async def payment_delete(request: Request, pid: int):
    f = await request.form()
    check_confirm(request, f)
    row = C(request).conn.execute("SELECT document_id FROM payments WHERE id = ?", (pid,)).fetchone()
    pay.delete_payment(C(request), pid, f.get("begruendung", ""))
    return go(f"/dokumente/{row['document_id']}" if row and row["document_id"] else "/auswertungen", "Zahlung gelöscht")


@router.post("/dokumente/{did}/status")
async def doc_status(request: Request, did: int):
    f = await request.form()
    docs.set_status(C(request), did, f.get("status", ""))
    return go(f"/dokumente/{did}", "Status geändert")


@router.post("/dokumente/{did}/rechnung")
async def doc_invoice_from_offer(request: Request, did: int):
    f = await request.form()
    new = docs.invoice_from_offer(C(request), did, f.get("art", "voll"), f.get("prozent"))
    return go(f"/dokumente/{new}", "Rechnungsentwurf angelegt")


@router.post("/dokumente/{did}/erste-rechnung")
async def doc_first_invoice(request: Request, did: int):
    new = pipeline.first_invoice(C(request), did)
    return go(f"/dokumente/{new}", "Rechnungsentwurf nach Zahlungsmodus angelegt")


@router.post("/dokumente/{did}/kopie")
async def doc_copy(request: Request, did: int):
    return go(f"/dokumente/{docs.copy_document(C(request), did)}", "Kopie angelegt")


@router.post("/dokumente/{did}/als-position")
async def doc_line_to_catalog(request: Request, did: int):
    ctx = C(request)
    f = await request.form()
    docs.save_draft(ctx, parse_editor(f), did)
    docs.save_line_as_catalog(ctx, did, int(f.get("position", "0")))
    return go(f"/dokumente/{did}", "Als Standardposition gespeichert")


# -- Belege -------------------------------------------------------------------
@router.get("/belege")
async def receipt_list(request: Request, status: str = "eingang", jahr: str = ""):
    items = rc.list_receipts(C(request), status or None, int(jahr) if jahr.isdigit() else None)
    return render(request, "receipts.html", {"nav": "belege", "items": items, "status": status, "jahr": jahr,
                                             "categories": md.list_categories(C(request))})


@router.post("/belege/upload")
async def receipt_upload(request: Request):
    ctx = C(request)
    f = await request.form()
    new = dup = 0
    for up in f.getlist("dateien"):
        if isinstance(up, UploadFile) and up.filename:
            r = rc.add_file(ctx, await up.read(), up.filename, "upload")
            dup, new = (dup + 1, new) if r["duplicate"] else (dup, new + 1)
    return go("/belege", f"{new} neu, {dup} schon vorhanden")


@router.post("/belege/eigenbeleg")
async def receipt_own(request: Request):
    f = await request.form()
    r = rc.create_own_receipt(C(request), {k: f.get(k, "") for k in (
        "receipt_date", "supplier", "description", "gross", "tax_kind", "category_id", "paid_on", "paid_via", "reason")})
    return go(f"/belege/{r['id']}", "Eigenbeleg angelegt, bitte prüfen und bestätigen")


@router.post("/belege/abholen")
async def receipt_fetch(request: Request):
    try:
        r = pull(C(request), SshTransport())
    except (subprocess.CalledProcessError, OSError):
        return go("/belege", "Server nicht erreichbar, nichts abgeholt")
    msg = f"{len(r['neu'])} abgeholt, {len(r['dubletten'])} schon vorhanden"
    if r["fehler"]:
        msg += f", {len(r['fehler'])} Fehler: " + "; ".join(r["fehler"])[:300]
    return go("/belege", msg)


@router.get("/belege/{rid}")
async def receipt_show(request: Request, rid: int):
    ctx = C(request)
    r = rc.get_receipt(ctx, rid)
    suffix = r["file_path"].rsplit(".", 1)[-1].lower()
    parts = rc.receipt_parts(ctx, rid) if r["tax_kind"] is None else []
    return render(request, "receipt.html", {"nav": "belege", "r": r, "suffix": suffix, "parts": parts,
                                            "categories": md.list_categories(ctx)})


@router.get("/belege/{rid}/datei")
async def receipt_file(request: Request, rid: int):
    path = rc.receipt_file(C(request), rid)
    if path.suffix.lower() == ".heic":
        import pillow_heif
        from PIL import Image
        pillow_heif.register_heif_opener()
        buf = io.BytesIO()
        Image.open(path).convert("RGB").save(buf, "JPEG", quality=85)
        return Response(buf.getvalue(), media_type="image/jpeg")
    media = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
             ".xml": "text/plain; charset=utf-8"}.get(path.suffix.lower(), "application/octet-stream")
    return Response(path.read_bytes(), media_type=media)


def _receipt_fields(f) -> dict:
    fields = {k: f.get(k) for k in rc.FIELDS if k in f}
    if not fields.get("tax_kind"):
        fields.pop("tax_kind", None)
    parts = [{"tax_kind": k, "gross": g} for k, g in zip(f.getlist("part_kind"), f.getlist("part_gross")) if g.strip()]
    if parts:                                   # Aufteilung auf mehrere Steuersätze ersetzt Steuerart + Brutto
        fields["parts"] = parts
        fields.pop("tax_kind", None)
        fields.pop("gross", None)
    return fields


@router.post("/belege/{rid}")
async def receipt_save(request: Request, rid: int):
    rc.update(C(request), rid, _receipt_fields(await request.form()))
    return go(f"/belege/{rid}", "Gespeichert")


@router.post("/belege/{rid}/bestaetigen")
async def receipt_confirm(request: Request, rid: int):
    ctx = C(request)
    rc.update(ctx, rid, _receipt_fields(await request.form()))
    rc.confirm(ctx, rid)
    nxt = rc.list_receipts(ctx, "eingang")
    return go(f"/belege/{nxt[-1]['id']}" if nxt else "/belege?status=geprueft", "Bestätigt")


@router.post("/belege/{rid}/loeschen")
async def receipt_delete(request: Request, rid: int):
    check_confirm(request, await request.form())
    rc.delete(C(request), rid)
    return go("/belege", "Beleg gelöscht")


# -- Auswertungen -------------------------------------------------------------
@router.get("/auswertungen")
async def reports(request: Request, zeitraum: str = "", jahr: str = ""):
    ctx = C(request)
    mode = md.get_settings(ctx)["ustva_period"]
    today = ctx.now_date()
    year = int(jahr) if jahr.isdigit() else today.year
    spec = zeitraum or period_of(today, mode)
    periods = list(dict.fromkeys(period_of(date(year, m, 1), mode) for m in range(1, 13)))
    return render(request, "reports.html", {"nav": "auswertungen", "u": ustva(ctx, spec), "e": euer(ctx, year),
                                            "periods": periods, "spec": spec, "year": year,
                                            "locks": per.locked_months(ctx)})


@router.get("/umsatzsteuer")
async def vat_overview(request: Request, jahr: str = ""):
    ctx = C(request)
    today = ctx.now_date()
    year = int(jahr) if jahr.isdigit() else today.year
    return render(request, "umsatzsteuer.html", {"nav": "umsatzsteuer", "j": jahresuebersicht(ctx, year),
                                                 "years": list(range(today.year, today.year - 4, -1))})


@router.post("/auswertungen/ust-zahlung")
async def reports_tax_payment(request: Request):
    f = await request.form()
    pay.record_tax_payment(C(request), f.get("datum", ""), f.get("betrag", ""), f.get("notiz", ""))
    return go("/auswertungen", "Zahlung an das Finanzamt erfasst")


@router.post("/auswertungen/sperren")
async def reports_lock(request: Request):
    f = await request.form()
    check_confirm(request, f)
    per.lock_month(C(request), f.get("monat", ""), f.get("notiz", ""))
    return go("/auswertungen", "Monat abgeschlossen")


@router.post("/auswertungen/entsperren")
async def reports_unlock(request: Request):
    f = await request.form()
    check_confirm(request, f)
    per.unlock_month(C(request), f.get("monat", ""), f.get("begruendung", ""))
    return go("/auswertungen", "Monat wieder offen")


# -- Abgaben (Spec 18.9) -----------------------------------------------------
def _abgaben(request: Request, **extra):
    from ..elster.ebilanz import TAXONOMIE
    from ..services import elster
    ctx = C(request)
    jahre = []
    for y in elster.jahre(ctx):
        try:
            jahre.append(elster.kennzahlen(ctx, y))
        except KontorError as e:
            jahre.append({"jahr": y, "fehler": str(e)})
    return render(request, "abgaben.html", {"nav": "abgaben", "jahre": jahre, "taxonomie": TAXONOMIE,
                                           "s": md.get_settings(ctx), "uebermittlungen": elster.uebermittlungen(ctx),
                                           "heute_jahr": ctx.now_date().year, **extra})


@router.get("/abgaben")
async def abgaben(request: Request):
    return _abgaben(request)


@router.post("/abgaben/{jahr}/pruefen")
async def abgaben_pruefen(request: Request, jahr: int):
    from ..services import elster
    try:
        return _abgaben(request, pruefung=elster.pruefen(C(request), jahr))
    except KontorError as e:
        return _abgaben(request, fehler=str(e))


@router.post("/abgaben/senden")
async def abgaben_senden(request: Request):
    from ..services import elster
    f = await request.form()
    check_confirm(request, f)
    jahr = f.get("jahr", "")
    if not jahr.isdigit():
        raise KontorError("Jahr wählen.")
    if not f.get("datenschutz"):
        return _abgaben(request, fehler="Vor dem Senden die Datenschutzhinweise der Finanzverwaltung lesen und bestätigen "
                                         "(ELSTER-Entwicklerbereich).")
    try:
        result = elster.senden(C(request), int(jahr), f.get("pin", ""), test=f.get("art") != "echt")
    except KontorError as e:
        return _abgaben(request, fehler=str(e))
    return _abgaben(request, ergebnis=result)


@router.get("/export/journal/{jahr}.csv")
async def export_journal(request: Request, jahr: int):
    return Response(journal_csv(C(request), jahr), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="journal-{jahr}.csv"'})
