"""Angebots-Pipeline: Übersicht nach Stand, Ablauf der Gültigkeit mit Nachfass-Aufgabe, erste Rechnung nach
Zahlungsmodus."""
from __future__ import annotations

from datetime import datetime

from ..context import Ctx
from ..core.errors import KontorError
from ..store import audit
from ..store.db import tx
from . import documents as docs

PAYMENT_MODES = {"vorab": "vorab bei Auftrag", "danach": "nach Fertigstellung",
                 "50_50": "50 % bei Auftrag, 50 % nach Fertigstellung"}


def expire_offers(ctx: Ctx) -> list[int]:
    """Verschickte Angebote, deren Gültigkeit verstrichen ist, gelten als abgelehnt; je Angebot eine
    Nachfass-Aufgabe („abgelaufen“), damit es noch zu retten ist."""
    today = ctx.now_date().isoformat()
    ids = [r[0] for r in ctx.conn.execute(
        "SELECT id FROM documents WHERE kind = 'angebot' AND status = 'abgeschlossen' AND valid_until < ?", (today,))]
    for did in ids:
        with tx(ctx.conn):
            docs.set_status(ctx, did, "abgelehnt")
            ctx.conn.execute("INSERT INTO followups (document_id, reason, created_at) VALUES (?, 'abgelaufen', ?)",
                             (did, datetime.now().isoformat(timespec="seconds")))
            audit.log(ctx.conn, ctx.actor, "angebot_abgelaufen", "dokument", did, {"heute": today})
    return ids


def open_followups(ctx: Ctx) -> list[dict]:
    rows = ctx.conn.execute(
        "SELECT f.*, d.number, d.customer_json, d.net, d.valid_until, d.status FROM followups f "
        "JOIN documents d ON d.id = f.document_id WHERE f.done_at IS NULL ORDER BY f.created_at")
    return [dict(r) for r in rows]


def close_followup(ctx: Ctx, followup_id: int, note: str = "") -> None:
    with tx(ctx.conn):
        cur = ctx.conn.execute("UPDATE followups SET done_at = ?, note = ? WHERE id = ? AND done_at IS NULL",
                               (datetime.now().isoformat(timespec="seconds"), note, followup_id))
        if not cur.rowcount:
            raise KontorError(f"Aufgabe {followup_id} gibt es nicht oder sie ist schon erledigt.")
        audit.log(ctx.conn, ctx.actor, "nachfassen_erledigt", "aufgabe", followup_id, {"notiz": note})


def board(ctx: Ctx) -> dict:
    offers = docs.list_documents(ctx, group="angebote")
    lanes = {"entwurf": [], "verschickt": [], "angenommen": [], "abgelehnt": []}
    for d in offers:
        lane = {"abgeschlossen": "verschickt"}.get(d["status"], d["status"])
        lanes.setdefault(lane, []).append(d)
    return {**lanes, "sums": {k: sum(d["net"] for d in v) for k, v in lanes.items()}}


def first_invoice(ctx: Ctx, offer_id: int) -> int:
    """Rechnungsentwurf passend zum Zahlungsmodus: 50/50 → Abschlag 50 %, sonst Rechnung über alles."""
    o = docs.get_document(ctx, offer_id)
    if o["payment_mode"] == "50_50":
        return docs.invoice_from_offer(ctx, offer_id, "abschlag", "50")
    return docs.invoice_from_offer(ctx, offer_id, "voll")
