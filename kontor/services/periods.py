"""Monatsabschluss: sperrt Zahlungen und geprüfte Belege eines Monats (Trigger in 0001)."""
from __future__ import annotations

import re
from datetime import datetime

from ..context import Ctx, require_human
from ..core.errors import KontorError
from ..store import audit
from ..store.db import tx


def _month(month: str) -> str:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise KontorError(f"Monat bitte als JJJJ-MM, nicht {month!r}.")
    return month


def lock_month(ctx: Ctx, month: str, note: str = "") -> None:
    require_human(ctx)
    m = _month(month)
    with tx(ctx.conn):
        ctx.conn.execute("INSERT OR IGNORE INTO period_locks (month, locked_at, note) VALUES (?,?,?)",
                         (m, datetime.now().isoformat(timespec="seconds"), note))
        audit.log(ctx.conn, ctx.actor, "monat_sperren", "monat", None, {"monat": m, "notiz": note})


def unlock_month(ctx: Ctx, month: str, reason: str) -> None:
    require_human(ctx)
    m = _month(month)
    if not (reason or "").strip():
        raise KontorError("Zum Entsperren eine Begründung angeben.")
    with tx(ctx.conn):
        ctx.conn.execute("DELETE FROM period_locks WHERE month = ?", (m,))
        audit.log(ctx.conn, ctx.actor, "monat_entsperren", "monat", None, {"monat": m, "begruendung": reason})


def locked_months(ctx: Ctx) -> list[dict]:
    return [dict(r) for r in ctx.conn.execute("SELECT * FROM period_locks ORDER BY month")]
