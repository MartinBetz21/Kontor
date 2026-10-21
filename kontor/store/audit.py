"""Protokoll, nur anhängend, mit Hash-Kette: jeder Eintrag hasht seinen Vorgänger mit."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime

GENESIS = "0" * 64


def _canon(data) -> str:
    return json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _hash(prev: str, at: str, actor: str, action: str, obj: str, obj_id, payload: str) -> str:
    body = _canon({"at": at, "actor": actor, "action": action, "object": obj,
                   "object_id": obj_id, "payload": payload})
    return hashlib.sha256((prev + body).encode("utf-8")).hexdigest()


def log(conn: sqlite3.Connection, actor: str, action: str, obj: str, obj_id: int | None, payload: dict) -> str:
    row = conn.execute("SELECT hash FROM audit_log ORDER BY id DESC LIMIT 1").fetchone()
    prev = row[0] if row else GENESIS
    at = datetime.now().isoformat(timespec="seconds")
    body = _canon(payload)
    h = _hash(prev, at, actor, action, obj, obj_id, body)
    conn.execute(
        "INSERT INTO audit_log (at, actor, action, object, object_id, payload, prev_hash, hash) VALUES (?,?,?,?,?,?,?,?)",
        (at, actor, action, obj, obj_id, body, prev, h),
    )
    return h


def verify(conn: sqlite3.Connection) -> list[str]:
    problems, prev = [], GENESIS
    for r in conn.execute("SELECT * FROM audit_log ORDER BY id"):
        if r["prev_hash"] != prev:
            problems.append(f"Protokoll #{r['id']}: Kette unterbrochen")
        expected = _hash(r["prev_hash"], r["at"], r["actor"], r["action"], r["object"], r["object_id"], r["payload"])
        if expected != r["hash"]:
            problems.append(f"Protokoll #{r['id']}: Inhalt verändert")
        prev = r["hash"]
    return problems
