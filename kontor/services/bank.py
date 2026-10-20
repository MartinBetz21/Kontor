"""Bank als Quelle der Buchungen (Spec 18.7a): Konten, Import, interne Umbuchungen, Zuordnung, Regeln, Saldo-Anker."""
from __future__ import annotations

import json
import re
from datetime import date

from ..context import Ctx
from ..core.errors import KontorError
from ..importers.datev import parse_datev
from ..importers.lexware import parse_kontenuebersicht
from ..store import audit
from ..store.db import tx
from . import documents as docs
from . import masterdata as md
from . import payments as pay

TAX_KEYS = ("19", "7", "0", "rc_eu", "rc_drittland")
KINDS = ("rechnung", "konto", "intern", "beleg")


# -- Konten -------------------------------------------------------------------------
def add_account(ctx: Ctx, iban: str, name: str, account: str) -> None:
    iban = iban.replace(" ", "").upper()
    if not re.fullmatch(r"[A-Z]{2}\d{2}[A-Z0-9]{10,30}", iban) or not re.fullmatch(r"18\d\d", account):
        raise KontorError("IBAN und SKR04-Bankkonto (18xx) angeben.")
    with tx(ctx.conn):
        ctx.conn.execute("INSERT INTO bank_accounts (iban, name, account) VALUES (?, ?, ?) ON CONFLICT(iban) DO "
                         "UPDATE SET name = excluded.name, account = excluded.account", (iban, name, account))
        audit.log(ctx.conn, ctx.actor, "bankkonto", "bank_accounts", None, {"iban": iban, "konto": account})


def accounts(ctx: Ctx) -> dict[str, dict]:
    return {r["iban"]: dict(r) for r in ctx.conn.execute("SELECT * FROM bank_accounts ORDER BY account")}


def _own_names(ctx: Ctx) -> list[str]:
    return [re.escape(n.strip()).replace(r"\ ", r"\s*") for n in md.get_settings(ctx)["own_names"].split(",") if n.strip()]


# -- Import -------------------------------------------------------------------------
def import_datev(ctx: Ctx, text: str) -> dict:
    own = accounts(ctx)
    rows = parse_datev(text, set(own), _own_names(ctx))
    unknown = sorted({r["iban"] for r in rows} - set(own))
    if unknown:
        raise KontorError(f"Unbekanntes Bankkonto im Export: {', '.join(unknown)}. Erst `kontor bank konto` anlegen.")
    added = 0
    with tx(ctx.conn):
        for r in rows:
            cur = ctx.conn.execute(
                "INSERT OR IGNORE INTO bank_transactions (account, booked_on, amount, counterparty, purpose, external_id, "
                "raw_json, duplicate, internal, counterparty_iban) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (r["iban"], r["date"], r["amount"], r["counterparty"], r["purpose"], r["external_id"],
                 json.dumps(r["raw"], ensure_ascii=False), int(r["duplicate"]), int(r["internal"]), r["counterparty_iban"]))
            added += cur.rowcount
        pairs = _pair_internal(ctx)
        audit.log(ctx.conn, ctx.actor, "bank_import", "bank", None,
                  {"zeilen": len(rows), "neu": added, "dubletten": sum(r["duplicate"] for r in rows), "paare": pairs})
    return {"zeilen": len(rows), "neu": added, "dubletten": sum(r["duplicate"] for r in rows), "paare_neu": pairs}


def _pair_internal(ctx: Ctx) -> int:
    """Gegenbuchung suchen, in beide Richtungen. Gepaarte Umsätze werden als intern zugeordnet."""
    txs = [dict(r) for r in ctx.conn.execute(
        "SELECT * FROM bank_transactions WHERE duplicate = 0 AND pair_id IS NULL ORDER BY booked_on, id")]
    norm = lambda s: " ".join((s or "").split()).lower()   # noqa: E731
    paired, used = 0, set()
    for t in txs:
        if t["id"] in used or not t["internal"]:
            continue
        for o in txs:
            if o["id"] in used or o["id"] == t["id"] or o["account"] == t["account"] or o["amount"] != -t["amount"]:
                continue
            if abs((date.fromisoformat(o["booked_on"]) - date.fromisoformat(t["booked_on"])).days) > 5:
                continue
            # Vom Abgang aus genügt die Gegenbuchung; vom Zugang aus (manche Banken: der Abgang trägt nur den
            # Inhabernamen) muss der Name übereinstimmen oder das Gegenkonto eine eigene IBAN sein.
            names_other = t["counterparty"].replace(" ", "") == o["account"] or t["counterparty_iban"] == o["account"]
            if t["amount"] > 0 and not (o["internal"] or names_other or norm(o["counterparty"]) == norm(t["counterparty"])
                                        or o["counterparty_iban"] == t["account"]):
                continue
            for a, b in ((t, o), (o, t)):
                ctx.conn.execute("UPDATE bank_transactions SET internal = 1, pair_id = ? WHERE id = ?", (b["id"], a["id"]))
                if not ctx.conn.execute("SELECT 1 FROM bank_assignments WHERE bank_transaction_id = ?", (a["id"],)).fetchone():
                    ctx.conn.execute("INSERT INTO bank_assignments (bank_transaction_id, kind, account, created_by, note) "
                                     "VALUES (?, 'intern', '1460', 'system', 'Gegenbuchung erkannt')", (a["id"],))
            used |= {t["id"], o["id"]}
            paired += 1
            break
    return paired


def confirm_duplicate(ctx: Ctx, tx_id: int, note: str) -> None:
    """Eine als Dublette markierte Zeile ist laut Kontoauszug doch eine echte zweite Zahlung."""
    if not note.strip():
        raise KontorError("Begründung fehlt (z. B. „Kontoauszug zeigt die Zahlung zweimal“).")
    with tx(ctx.conn):
        n = ctx.conn.execute("UPDATE bank_transactions SET duplicate = 0 WHERE id = ? AND duplicate = 1", (tx_id,)).rowcount
        if not n:
            raise KontorError(f"Umsatz {tx_id} ist keine markierte Dublette.")
        audit.log(ctx.conn, ctx.actor, "bank_dublette_echt", "bank", tx_id, {"grund": note})
        _pair_internal(ctx)                        # eine echte zweite Umbuchung findet jetzt ihr Gegenstück


def transactions(ctx: Ctx, year: int | None = None, offen: bool = False) -> list[dict]:
    sql = ("SELECT t.*, a.kind, a.account AS target_account, a.tax_key, a.document_id, a.receipt_id, a.note AS a_note, "
           "b.account AS bank_account, b.name AS bank_name FROM bank_transactions t "
           "LEFT JOIN bank_assignments a ON a.bank_transaction_id = t.id JOIN bank_accounts b ON b.iban = t.account WHERE 1=1")
    args: list = []
    if year:
        sql += " AND substr(t.booked_on, 1, 4) = ?"
        args.append(str(year))
    if offen:
        sql += " AND a.bank_transaction_id IS NULL AND t.duplicate = 0"
    return [dict(r) for r in ctx.conn.execute(sql + " ORDER BY t.booked_on, t.id", args)]


# -- Zuordnung ----------------------------------------------------------------------
def assign(ctx: Ctx, tx_id: int, kind: str, *, document_id: int | None = None, account: str = "",
           tax_key: str = "", receipt_id: int | None = None, note: str = "",
           document_ids: list[int] | None = None) -> None:
    """kind 'rechnung' mit document_ids: Sammelzahlung, der Betrag deckt genau die offenen Reste dieser Rechnungen."""
    t = ctx.conn.execute("SELECT * FROM bank_transactions WHERE id = ?", (tx_id,)).fetchone()
    if not t:
        raise KontorError(f"Umsatz {tx_id} gibt es nicht.")
    if t["duplicate"]:
        raise KontorError("Der Umsatz ist als Dublette markiert; erst bestätigen, dass er echt ist.")
    if ctx.conn.execute("SELECT 1 FROM bank_assignments WHERE bank_transaction_id = ?", (tx_id,)).fetchone():
        raise KontorError(f"Umsatz {tx_id} ist schon zugeordnet; erst lösen (ein Umsatz, ein Ziel).")
    if kind not in KINDS:
        raise KontorError(f"Art {kind!r}: {', '.join(KINDS)}.")
    payment_id = None
    with tx(ctx.conn):
        if kind == "rechnung":
            ids = [int(i) for i in (document_ids or ([document_id] if document_id else []))]
            if not ids:
                raise KontorError("Rechnung fehlt.")
            opens = [docs.get_document(ctx, i)["open"] for i in ids]
            if len(ids) > 1 and sum(opens) != t["amount"]:
                raise KontorError(f"Sammelzahlung {t['amount'] / 100:.2f} € deckt die offenen Reste "
                                  f"({sum(opens) / 100:.2f} €) nicht genau.")
            for i, rest in zip(ids, opens):
                pid = pay.record_payment(ctx, i, t["booked_on"], t["amount"] if len(ids) == 1 else rest,
                                         f"Bank {t['counterparty']}".strip())
                ctx.conn.execute("UPDATE payments SET source = 'bank', bank_transaction_id = ? WHERE id = ?", (tx_id, pid))
                payment_id = payment_id or pid
            document_id = ids[0]
            note = note or (", ".join(f"#{i}" for i in ids) if len(ids) > 1 else "")
        elif kind in ("konto", "beleg"):
            if not re.fullmatch(r"\d{3,5}", account or "") or tax_key not in TAX_KEYS:
                raise KontorError(f"Konto (SKR04) und Steuerschlüssel angeben ({', '.join(TAX_KEYS)}).")
            if kind == "beleg" and not receipt_id:
                raise KontorError("Beleg fehlt.")
        elif kind == "intern":
            account = "1460"
        ctx.conn.execute("INSERT INTO bank_assignments (bank_transaction_id, kind, document_id, payment_id, receipt_id, "
                         "account, tax_key, note, created_by) VALUES (?,?,?,?,?,?,?,?,?)",
                         (tx_id, kind, document_id, payment_id, receipt_id, account, tax_key, note, ctx.actor))
        audit.log(ctx.conn, ctx.actor, "bank_zuordnung", "bank", tx_id,
                  {"art": kind, "dokument": document_id, "konto": account, "steuer": tax_key})


def unassign(ctx: Ctx, tx_id: int, reason: str) -> None:
    a = ctx.conn.execute("SELECT * FROM bank_assignments WHERE bank_transaction_id = ?", (tx_id,)).fetchone()
    if not a:
        raise KontorError(f"Umsatz {tx_id} ist nicht zugeordnet.")
    if not reason.strip():
        raise KontorError("Begründung fehlt.")
    with tx(ctx.conn):
        ctx.conn.execute("DELETE FROM bank_assignments WHERE bank_transaction_id = ?", (tx_id,))
        for (pid,) in ctx.conn.execute("SELECT id FROM payments WHERE bank_transaction_id = ?", (tx_id,)).fetchall():
            pay.delete_payment(ctx, pid, f"Bankzuordnung gelöst: {reason}")
        audit.log(ctx.conn, ctx.actor, "bank_zuordnung_loesen", "bank", tx_id, {"grund": reason})


def link_receipts(ctx: Ctx, year: int) -> int:
    """Geprüfte Belege mit dem Bankabgang verbinden: Betrag gleich, Abbuchung 5 Tage vor bis 90 Tage nach dem
    Belegdatum (Anwälte und Notare werden oft spät bezahlt). Aus der Konto-Zuordnung wird eine Beleg-Zuordnung (Vorsteuer dann auf den Cent aus dem Beleg)."""
    linked = 0
    used = {r[0] for r in ctx.conn.execute("SELECT receipt_id FROM bank_assignments WHERE receipt_id IS NOT NULL")}
    for r in ctx.conn.execute("SELECT * FROM receipts WHERE status = 'geprueft' AND gross IS NOT NULL "
                              "AND substr(receipt_date, 1, 4) = ? ORDER BY receipt_date", (str(year),)).fetchall():
        if r["id"] in used:
            continue
        day = date.fromisoformat(r["receipt_date"])
        for t in transactions(ctx, year):
            if t["kind"] != "konto" or t["receipt_id"] or t["amount"] != -r["gross"]:
                continue
            if -5 <= (date.fromisoformat(t["booked_on"]) - day).days <= 90:
                ctx.conn.execute("UPDATE bank_assignments SET kind = 'beleg', receipt_id = ? WHERE bank_transaction_id = ?",
                                 (r["id"], t["id"]))
                audit.log(ctx.conn, ctx.actor, "bank_beleg", "bank", t["id"], {"beleg": r["id"]})
                used.add(r["id"])
                linked += 1
                break
    return linked


# -- Regeln ---------------------------------------------------------------------------
STOP_WORDS = {"VISA", "SEPA", "LASTSCHRIFT", "KARTENZAHLUNG", "GMBH", "HTTPS", "WWW", "RECHNUNG", "ANGEBOT", "ZAHLUNG",
              "UEBERWEISUNG", "ÜBERWEISUNG", "GUTSCHRIFT", "DEUTSCHE", "EXTRA", "HOTEL", "KOELN", "KÖLN", "BERLIN",
              "LEISTG", "VERTRIEB", "KUNDE", "AUFTRAG", "BETRAG", "DANKE", "KAUFUMSATZ", "UMBUCHUNG", "SMART", "SPLIT"}


def _key(text: str) -> str:
    """Kennwort einer Gegenpartei: erstes Wort aus Buchstaben (≥ 4 Zeichen) in Großbuchstaben."""
    for w in re.findall(r"[A-Za-zÄÖÜäöü][A-Za-zÄÖÜäöü\-]{3,}", text or ""):
        if w.upper() not in STOP_WORDS:
            return w.upper()
    return ""


def _foreign(ctx: Ctx, text: str) -> bool:
    hay = (text or "").upper()
    return any(p.strip() and p.strip().upper() in hay for p in md.get_settings(ctx)["foreign_providers"].split(","))


def add_rule(ctx: Ctx, pattern: str, account: str, tax_key: str, source: str = "manuell") -> None:
    if tax_key not in TAX_KEYS or not re.fullmatch(r"\d{3,5}", account) or len(pattern.strip()) < 3:
        raise KontorError("Muster (≥ 3 Zeichen), Konto und Steuerschlüssel angeben.")
    with tx(ctx.conn):
        ctx.conn.execute("INSERT INTO bank_rules (pattern, account, tax_key, source) VALUES (?, ?, ?, ?) ON CONFLICT(pattern) "
                         "DO UPDATE SET account = excluded.account, tax_key = excluded.tax_key, source = excluded.source",
                         (pattern.strip().upper(), account, tax_key, source))


def learn_rules(ctx: Ctx, text: str) -> int:
    """Aus einem Lexware-Kontenexport (z. B. 2025): Bankzeile → Gegenkonto und Steuer. Nur Erfolgskonten und Anlagen;
    Personen-, Verrechnungs- und Bankkonten nicht. Ausländische Anbieter bekommen § 13b statt 19 %.
    Die Datei wird nur gelesen, nicht ins Journal übernommen."""
    rows = parse_kontenuebersicht(text)
    tax_of = {(r["date"], r["account"], r["text"]): r["tax_rate"] for r in rows}
    seen: dict[str, dict[tuple[str, str], int]] = {}
    for r in rows:
        contra = r["contra"] or ""
        if not re.fullmatch(r"18\d\d", r["account"]) or not re.fullmatch(r"(135|670|690|[4-7]\d{3})", contra):
            continue
        k = _key(r["text"])
        if not k:
            continue
        rate = tax_of.get((r["date"], contra, r["text"]), "")
        expense = contra[:1] in "567" or len(contra) == 3     # § 13b nur für bezogene Leistungen, nie für Erträge
        tax = "rc_eu" if expense and _foreign(ctx, r["text"]) else {"19": "19", "7": "7"}.get(rate, "0")
        seen.setdefault(k, {}).setdefault((contra, tax), 0)
        seen[k][(contra, tax)] += 1
    # Nur Wiederkehrendes wird Regel: mindestens zweimal dasselbe Konto. Einmalige Vorgänge (Restaurant, Hotel,
    # Behörde) bleiben Einzelentscheidung.
    learned = {}
    for k, counts in seen.items():
        (target, n) = max(counts.items(), key=lambda kv: kv[1])
        if n >= 2:
            learned[k] = target
    for k, (acc, tax) in learned.items():
        add_rule(ctx, k, acc, tax, "lexware")
    return len(learned)


def _fits_sign(account: str, amount: int) -> bool:
    """Aufwand (5–7xxx) und Anlagen nur für Abgänge, Erlöse (4xxx) nur für Eingänge."""
    if account[:1] in "567" or len(account) == 3:
        return amount < 0
    if account[:1] == "4":
        return amount > 0
    return True


def suggest(ctx: Ctx, t: dict) -> dict | None:
    hay = f"{t['counterparty']} {t['purpose']}".upper()
    if t["amount"] > 0:
        # 1. Rechnungsnummer im Verwendungszweck, Betrag passt zum offenen Rest
        for num in re.findall(r"RE-\d{4}-\d{3,4}", hay):
            d = ctx.conn.execute("SELECT id FROM documents WHERE number = ?", (num,)).fetchone()
            if d and 0 < t["amount"] <= docs.get_document(ctx, d["id"])["open"]:
                return {"kind": "rechnung", "document_id": d["id"], "regel": num}
        # 2. Betrag gleich dem offenen Rest von Rechnungen genau eines Kunden: die älteste (vor dem Zahlungstag)
        hits = [dict(r) for r in ctx.conn.execute(
            "SELECT d.id, d.number, d.customer_id, d.gross - d.prepaid_gross - COALESCE((SELECT SUM(amount) FROM payments "
            "WHERE document_id = d.id), 0) AS rest FROM documents d WHERE d.kind <> 'angebot' AND d.status IN "
            "('abgeschlossen', 'teilbezahlt') AND d.doc_date <= ? ORDER BY d.doc_date, d.number", (t["booked_on"],))]
        hits = [h for h in hits if h["rest"] == t["amount"]]
        if hits and len({h["customer_id"] for h in hits}) == 1:
            return {"kind": "rechnung", "document_id": hits[0]["id"], "regel": f"Betrag {hits[0]['number']}"}
    for r in ctx.conn.execute("SELECT * FROM bank_rules ORDER BY length(pattern) DESC"):
        if r["pattern"] in hay and _fits_sign(r["account"], t["amount"]):
            tax = "rc_eu" if r["tax_key"] == "19" and t["amount"] < 0 and _foreign(ctx, hay) else r["tax_key"]
            return {"kind": "konto", "account": r["account"], "tax_key": tax, "regel": r["pattern"]}
    return None


def apply_suggestions(ctx: Ctx, year: int) -> int:
    n = 0
    for t in transactions(ctx, year, offen=True):
        s = suggest(ctx, t)
        if s:
            assign(ctx, t["id"], s["kind"], document_id=s.get("document_id"), account=s.get("account", ""),
                   tax_key=s.get("tax_key", ""), note=f"Regel {s['regel']}")
            ctx.conn.execute("UPDATE bank_rules SET hits = hits + 1 WHERE pattern = ?", (s["regel"],))
            n += 1
    return n


# -- Saldo-Anker ----------------------------------------------------------------------
def set_anchor(ctx: Ctx, iban: str, day: str, balance: int | str, note: str = "") -> None:
    iban = iban.replace(" ", "").upper()
    if iban not in accounts(ctx):
        raise KontorError(f"Bankkonto {iban} ist nicht angelegt.")
    with tx(ctx.conn):
        ctx.conn.execute("INSERT INTO bank_anchors (iban, date, balance, note) VALUES (?, ?, ?, ?) ON CONFLICT(iban, date) "
                         "DO UPDATE SET balance = excluded.balance, note = excluded.note",
                         (iban, day, md.to_cents(balance), note))
        audit.log(ctx.conn, ctx.actor, "saldo_anker", "bank", None, {"iban": iban, "datum": day, "saldo": balance})


def anchors(ctx: Ctx) -> list[dict]:
    return [dict(r) for r in ctx.conn.execute("SELECT * FROM bank_anchors ORDER BY iban, date")]
