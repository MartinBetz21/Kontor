"""Bank-Schnittstelle (Spec 11). In Version 1 gibt es nur die Form; ein bankspezifischer Importer implementiert Importer."""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from ..context import Ctx
from ..store import audit
from ..store.db import tx


@dataclass(frozen=True)
class BankTransaction:
    account: str
    booked_on: str        # JJJJ-MM-TT
    amount: int           # Cent, Eingang positiv
    counterparty: str
    purpose: str
    external_id: str      # eindeutig je Bank, macht den Import wiederholbar
    raw: dict


class Importer(Protocol):
    name: str

    def fetch(self, since: date) -> list[BankTransaction]: ...


def store_transactions(ctx: Ctx, txs: list[BankTransaction]) -> int:
    added = 0
    with tx(ctx.conn):
        for t in txs:
            cur = ctx.conn.execute(
                "INSERT OR IGNORE INTO bank_transactions (account, booked_on, amount, counterparty, purpose, "
                "external_id, raw_json) VALUES (?,?,?,?,?,?,?)",
                (t.account, t.booked_on, t.amount, t.counterparty, t.purpose, t.external_id,
                 json.dumps(t.raw, ensure_ascii=False)))
            added += cur.rowcount
        if added:
            audit.log(ctx.conn, ctx.actor, "bank_import", "bank", None, {"neu": added})
    return added
