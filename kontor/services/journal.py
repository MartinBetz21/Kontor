"""Journal der Bilanz-Mandanten (Spec 18.8, 18.8a): Eröffnung aus der Saldenliste des Vorjahres, Import des
Lexware-Kontenexports, Abschluss- und Korrekturbuchungen mit Begründung, Salden, Jahressperre."""
from __future__ import annotations

import re

from ..context import Ctx, require_human
from ..core.bilanz import is_pl
from ..core.errors import KontorError
from ..importers.lexware import parse_kontenuebersicht, parse_saldenliste
from ..store import audit
from ..store.db import tx
from . import masterdata as md

VAT_TO_PRIOR_YEAR = ("3801", "3806", "3837", "1401", "1406", "1407")   # beim Vortrag → 3841; 3811/3816 bleiben
RESULT_CARRY = "2978"                           # Gewinn-/Verlustvortrag
ACCOUNT = re.compile(r"^\d{3,5}$")


def _require_balance_profile(ctx: Ctx) -> None:
    if md.get_profile(ctx)["gewinnermittlung"] != "bilanz":
        raise KontorError("Das Journal gibt es nur für Mandanten mit Bilanzierung (Profil gewinnermittlung=bilanz).")


def _locked(ctx: Ctx, year: int) -> bool:
    return ctx.conn.execute("SELECT 1 FROM journal_locks WHERE year = ?", (year,)).fetchone() is not None


def _check_open(ctx: Ctx, year: int) -> None:
    if _locked(ctx, year):
        raise KontorError(f"Das Jahr {year} ist gesperrt (Abschluss aufgestellt).")


def _names(ctx: Ctx, names: dict[str, str]) -> None:
    for number, name in names.items():
        ctx.conn.execute("INSERT INTO accounts (number, name) VALUES (?, ?) ON CONFLICT(number) DO UPDATE SET "
                         "name = CASE WHEN excluded.name <> '' THEN excluded.name ELSE accounts.name END", (number, name))


def opening_from_saldenliste(ctx: Ctx, year: int, text: str) -> int:
    """Vortrag zum 01.01.: Bestandskonten weiter, Ergebnis auf 2978, Umsatz- und Vorsteuer des Vorjahres auf 3841."""
    _require_balance_profile(ctx)
    _check_open(ctx, year)
    if ctx.conn.execute("SELECT 1 FROM journal WHERE year = ? AND art = 'eroeffnung'", (year,)).fetchone():
        raise KontorError(f"Die Eröffnung {year} ist schon gebucht.")
    bal, names = parse_saldenliste(text)
    if sum(bal.values()) != 0:
        raise KontorError(f"Die Saldenliste ist nicht ausgeglichen (Differenz {sum(bal.values())} Cent).")
    carry: dict[str, int] = {}
    for k, v in bal.items():
        if is_pl(k):
            carry[RESULT_CARRY] = carry.get(RESULT_CARRY, 0) + v
        elif k in VAT_TO_PRIOR_YEAR:
            carry["3841"] = carry.get("3841", 0) + v
        else:
            carry[k] = carry.get(k, 0) + v
    day = f"{year}-01-01"
    with tx(ctx.conn):
        _names(ctx, names)
        for k, v in sorted(carry.items()):
            if v:
                ctx.conn.execute("INSERT INTO journal (year, date, account, amount, text, art, ref) "
                                 "VALUES (?, ?, ?, ?, ?, 'eroeffnung', ?)", (year, day, k, v, "Eröffnung", f"EB-{year}"))
        audit.log(ctx.conn, ctx.actor, "journal_eroeffnung", "journal", None, {"jahr": year, "konten": len(carry)})
    return len([v for v in carry.values() if v])


def import_lexware(ctx: Ctx, year: int, text: str) -> int:
    """Ersetzt den Lexware-Import des Jahres durch den Inhalt der Datei (erneuter Export = neuer Stand)."""
    _require_balance_profile(ctx)
    _check_open(ctx, year)
    rows = parse_kontenuebersicht(text)
    other = sorted({r["date"][:4] for r in rows if r["date"][:4] != str(year)})
    if other:
        raise KontorError(f"Der Export enthält Buchungen aus {', '.join(other)}, erwartet war nur {year}.")
    with tx(ctx.conn):
        ctx.conn.execute("DELETE FROM journal WHERE year = ? AND art = 'import'", (year,))
        _names(ctx, {r["account"]: r["name"] for r in rows})
        for r in rows:
            ctx.conn.execute(
                "INSERT INTO journal (year, date, account, amount, contra, text, beleg, tax_rate, tax_account, art, ref) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'import', ?)",
                (year, r["date"], r["account"], r["amount"], r["contra"], r["text"], r["beleg"], r["tax_rate"],
                 r["tax_account"], f"lexware-{year}"))
        audit.log(ctx.conn, ctx.actor, "journal_import_lexware", "journal", None,
                  {"jahr": year, "zeilen": len(rows), "summe": sum(r["amount"] for r in rows)})
    return len(rows)


def book(ctx: Ctx, data: dict) -> str:
    """Abschluss- oder Korrekturbuchung: Kennung, Datum, Begründung, Konten → Betrag (Soll positiv); Summe null."""
    _require_balance_profile(ctx)
    ref, day, text = str(data.get("ref") or "").strip(), str(data.get("date") or ""), str(data.get("text") or "").strip()
    if not text:
        raise KontorError("Jede Buchung von Hand braucht eine Begründung (text).")
    if not ref:
        raise KontorError("Kennung fehlt (ref), z. B. „K1“.")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise KontorError("Datum als JJJJ-MM-TT.")
    lines = {str(k): md.to_cents(v) for k, v in (data.get("lines") or {}).items()}
    if len(lines) < 2 or any(not ACCOUNT.match(k) for k in lines):
        raise KontorError("Mindestens zwei Konten (Nummern) mit Betrag.")
    if sum(lines.values()) != 0:
        raise KontorError(f"Soll und Haben müssen sich zu null ergänzen (Differenz {sum(lines.values())} Cent).")
    year = int(day[:4])
    _check_open(ctx, year)
    if ctx.conn.execute("SELECT 1 FROM journal WHERE year = ? AND art = 'manuell' AND ref = ?", (year, ref)).fetchone():
        raise KontorError(f"Die Kennung {ref} gibt es {year} schon.")
    with tx(ctx.conn):
        for k, v in lines.items():
            if v:
                ctx.conn.execute("INSERT INTO journal (year, date, account, amount, text, art, ref) "
                                 "VALUES (?, ?, ?, ?, ?, 'manuell', ?)", (year, day, k, v, text, ref))
        audit.log(ctx.conn, ctx.actor, "journal_buchung", "journal", None, {"ref": ref, "datum": day, "zeilen": lines})
    return ref


def delete_manual(ctx: Ctx, year: int, ref: str, reason: str) -> None:
    _require_balance_profile(ctx)
    _check_open(ctx, year)
    if not reason.strip():
        raise KontorError("Begründung fehlt.")
    with tx(ctx.conn):
        n = ctx.conn.execute("DELETE FROM journal WHERE year = ? AND art = 'manuell' AND ref = ?", (year, ref)).rowcount
        if not n:
            raise KontorError(f"Keine Buchung {ref} in {year}.")
        audit.log(ctx.conn, ctx.actor, "journal_buchung_loeschen", "journal", None, {"ref": ref, "grund": reason})


def lexware_sourced(ctx: Ctx, year: int) -> bool:
    """Kommt das Journal dieses Jahres aus Lexware (18.8a)? Sonst aus Kontor selbst (18.7a); der Import ist dann Gegenprobe."""
    years = md.get_settings(ctx)["lexware_journal_years"]
    return str(year) in [y.strip() for y in years.split(",")]


def lines(ctx: Ctx, year: int, account: str | None = None, until: str | None = None) -> list[dict]:
    from .autojournal import auto_lines
    skip = "auto" if lexware_sourced(ctx, year) or _locked(ctx, year) else ""
    stored = [dict(r) for r in ctx.conn.execute(
        "SELECT * FROM journal WHERE year = ? AND art <> ? AND art <> ? ORDER BY date, id",
        (year, "import" if not lexware_sourced(ctx, year) else "", "auto" if not _locked(ctx, year) else ""))]
    computed = [] if skip else auto_lines(ctx, year)
    out = [r for r in stored + computed if (account is None or r["account"] == account) and (not until or r["date"] <= until)]
    return sorted(out, key=lambda r: (r["date"], r.get("id") or 0))


def balances(ctx: Ctx, year: int, until: str | None = None) -> dict[str, int]:
    bal: dict[str, int] = {}
    for r in lines(ctx, year, until=until):
        bal[r["account"]] = bal.get(r["account"], 0) + r["amount"]
    return {k: v for k, v in bal.items() if v}


def import_lines(ctx: Ctx, year: int) -> list[dict]:
    return [dict(r) for r in ctx.conn.execute("SELECT * FROM journal WHERE year = ? AND art = 'import' ORDER BY date, id", (year,))]


def account_names(ctx: Ctx) -> dict[str, str]:
    from ..core.bilanz import SKR04_NAMES
    names = dict(SKR04_NAMES)
    names.update({r[0]: r[1] for r in ctx.conn.execute("SELECT number, name FROM accounts") if r[1] and r[1] != r[0]})
    names.update({r[0]: r[1] for r in ctx.conn.execute("SELECT account, name FROM bank_accounts")})
    return names


def lock_year(ctx: Ctx, year: int, note: str) -> None:
    require_human(ctx)
    if not note.strip():
        raise KontorError("Vermerk fehlt (z. B. „Abschluss aufgestellt am …“).")
    from .autojournal import auto_lines
    frozen = [] if lexware_sourced(ctx, year) else auto_lines(ctx, year)
    with tx(ctx.conn):
        for r in frozen:                           # was Kontor abgeleitet hat, wird mit der Sperre festgeschrieben
            ctx.conn.execute("INSERT INTO journal (year, date, account, amount, contra, text, beleg, art, ref) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, 'auto', ?)",
                             (year, r["date"], r["account"], r["amount"], r["contra"], r["text"], r["beleg"], r["ref"]))
        ctx.conn.execute("INSERT INTO journal_locks (year, note) VALUES (?, ?)", (year, note.strip()))
        audit.log(ctx.conn, ctx.actor, "journal_sperren", "journal", None, {"jahr": year, "vermerk": note})
