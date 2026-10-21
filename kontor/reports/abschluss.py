"""Bilanz/GuV, Saldenliste und Auffälligkeiten aus dem Journal (Spec 18.8, 18.8a). Jede Auffälligkeit entspricht
einer bekannten Buchungsfalle und nennt die Buchungen, an denen sie hängt."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from ..context import Ctx
from ..core.bilanz import UST_ACCOUNTS, is_pl, positions
from ..core.errors import KontorError
from ..core.money import fmt_eur, round_cents
from ..services import journal as jr
from ..services import masterdata as md

CLEARING = {"1370": "Durchlaufende Posten", "1460": "Geldtransit", "1307": "Forderungen gegen Gesellschafter",
            "3500": "Noch nicht zugeordnete Bankvorgänge"}
REVENUE = ("4200", "4300", "4338", "4400")
RECEIVABLE = ("1200", "1307")


def _is_bank(account: str) -> bool:
    return len(account) == 4 and account.startswith("18")


def _capital(ctx: Ctx) -> tuple[int, int]:
    s = md.get_settings(ctx)
    if not s["share_capital"]:
        raise KontorError("Stammkapital fehlt in den Einstellungen (share_capital, z. B. 25000,00).")
    return md.to_cents(s["share_capital"]), md.to_cents(s["share_capital_outstanding"] or "0")


def bilanz_guv(ctx: Ctx, year: int, until: str | None = None) -> dict:
    bal = jr.balances(ctx, year, until)
    gez, ausst = _capital(ctx)
    names = jr.account_names(ctx)
    guv = sorted(({"konto": k, "name": names.get(k, ""), "betrag": -v} for k, v in bal.items() if is_pl(k)),
                 key=lambda r: (r["betrag"] < 0, -abs(r["betrag"])))
    bestand = sorted(({"konto": k, "name": names.get(k, ""), "saldo": v} for k, v in bal.items()
                      if not is_pl(k) and k not in UST_ACCOUNTS), key=lambda r: r["konto"])
    return {"jahr": year, "stichtag": until or f"{year}-12-31", "positionen": positions(bal, gez, ausst),
            "guv": guv, "bestand": bestand, "ust_saldo": sum(bal.get(k, 0) for k in UST_ACCOUNTS)}


def saldenliste_csv(ctx: Ctx, year: int, until: str | None = None) -> str:
    names = jr.account_names(ctx)
    out = ["﻿Konto;Bezeichnung;Saldo;S/H"]
    for k, v in sorted(jr.balances(ctx, year, until).items(), key=lambda kv: int(kv[0])):
        out.append(f"{k};{names.get(k, k)};{fmt_eur(abs(v)).replace(' €', '')};{'S' if v > 0 else 'H'}")
    return "\n".join(out) + "\n"


def _day(iso: str) -> date:
    return date.fromisoformat(iso)


def auffaelligkeiten(ctx: Ctx, year: int) -> list[dict]:
    rows = [r for r in jr.lines(ctx, year) if r["art"] != "eroeffnung"]
    bal = jr.balances(ctx, year)
    found: list[dict] = []

    # Nr. 6, 14: Verrechnungskonten müssen zum Stichtag null sein; ein Rest wird zerlegt, nicht ausgeglichen
    for k, name in CLEARING.items():
        if bal.get(k):
            found.append({"art": "verrechnungskonto", "konto": k, "betrag": bal[k],
                          "text": f"{k} {name} steht auf {fmt_eur(bal[k])} statt 0: zerlegen, "
                                  "nicht ausgleichen", "buchungen": [r for r in rows if r["account"] == k]})

    # Nr. 1: dieselbe Bankzeile zweimal im Export (Lexware bucht beide)
    seen: dict[tuple, list] = {}
    for r in rows:
        if _is_bank(r["account"]) and r["art"] == "import":
            seen.setdefault((r["account"], r["date"], r["amount"], r["text"]), []).append(r)
    for (k, d, amount, text), group in seen.items():
        if len(group) > 1:
            found.append({"art": "exportdublette", "konto": k, "betrag": amount,
                          "text": f"{d} {text}: {len(group)}× auf {k} — Kontoauszug entscheidet",
                          "buchungen": group})

    # Nr. 13: Vorsteuer auf Leistungen ausländischer Anbieter (§ 13b statt 19 %)
    providers = [p.strip().upper() for p in md.get_settings(ctx)["foreign_providers"].split(",") if p.strip()]
    for r in rows:
        if r["account"] in ("1401", "1406") and r["amount"] > 0 and r["art"] == "import":
            hay = f"{r['text']} {r['beleg']}".upper()
            hit = next((p for p in providers if p in hay), None)
            if hit:
                found.append({"art": "vorsteuer_ausland", "konto": r["account"], "betrag": r["amount"],
                              "text": f"{r['date']} {r['text']}: {fmt_eur(r['amount'])} Vorsteuer auf {hit} "
                                      " — § 13b, die Vorsteuer ist Kosten", "buchungen": [r]})

    # Nr. 12: Erlös direkt aus dem Bankeingang, die Rechnung über denselben Betrag zusätzlich als bezahlt
    for r in rows:
        if r["account"] in REVENUE and r["amount"] < 0 and _is_bank(r["contra"]):
            rate = Decimal(r["tax_rate"] or "0")
            gross = round_cents(Decimal(-r["amount"]) * (100 + rate) / 100)
            day = _day(r["date"])
            twin = [x for x in rows if x["account"] in RECEIVABLE and x["amount"] == -gross
                    and abs((_day(x["date"]) - day).days) <= 10]
            if twin:
                found.append({"art": "erloes_doppelt", "konto": r["account"], "betrag": gross,
                              "text": f"{r['date']} {r['text']}: Erlös aus dem Bankeingang und Rechnung "
                                      f"{twin[0]['beleg'] or twin[0]['text']} als bezahlt",
                              "buchungen": [r, *twin]})

    # Nr. 11: Abgang an den Inhaber (Einstellung owner_names) als Aufwand gebucht, obwohl das Geld auf einem eigenen
    # Konto ankam
    owners = [n.strip() for n in md.get_settings(ctx)["owner_names"].split(",") if n.strip()]
    for r in rows:
        owner = next((n for n in owners if n.upper() in r["text"].upper()), None)
        if _is_bank(r["account"]) and r["amount"] < 0 and owner \
                and not (_is_bank(r["contra"]) or r["contra"] in ("1460", "1370")):
            day = _day(r["date"])
            twin = [x for x in rows if _is_bank(x["account"]) and x["account"] != r["account"]
                    and x["amount"] == -r["amount"] and timedelta(0) <= _day(x["date"]) - day <= timedelta(days=5)]
            if twin:
                found.append({"art": "umbuchung_als_aufwand", "konto": r["contra"], "betrag": -r["amount"],
                              "text": f"{r['date']} {fmt_eur(-r['amount'])} an „{owner}“ auf {r['contra']} gebucht, "
                                      f"am {twin[0]['date']} auf {twin[0]['account']} eingegangen",
                              "buchungen": [r, *twin]})
    return found + _bank_findings(ctx, year)


def _bank_findings(ctx: Ctx, year: int) -> list[dict]:
    """Spec 18.7a: offene Umsätze, unbestätigte Dubletten, Saldo-Anker, Zahlungen ohne Bankbezug."""
    from ..services import bank
    found: list[dict] = []
    open_ = bank.transactions(ctx, year, offen=True)
    if open_:
        found.append({"art": "ohne_zuordnung", "konto": "3500", "betrag": sum(t["amount"] for t in open_),
                      "text": f"{len(open_)} Bankumsätze ohne Zuordnung, parken auf 3500",
                      "buchungen": [_as_line(t) for t in open_]})
    for t in bank.transactions(ctx, year):
        if t["duplicate"]:
            found.append({"art": "bank_dublette", "konto": t["bank_account"], "betrag": t["amount"],
                          "text": f"{t['booked_on']} {t['counterparty']} {fmt_eur(t['amount'])}: zweites gleiches "
                                  "Vorkommen im Export — zählt nicht, bis der Kontoauszug es bestätigt",
                          "buchungen": [_as_line(t)]})
    accounts = bank.accounts(ctx)
    for a in bank.anchors(ctx):
        if a["date"][:4] != str(year):
            continue
        acct = accounts[a["iban"]]["account"]
        computed = jr.balances(ctx, year, a["date"]).get(acct, 0)
        if computed != a["balance"]:
            found.append({"art": "saldo_anker", "konto": acct, "betrag": a["balance"] - computed,
                          "text": f"{acct} am {a['date']}: Auszug {fmt_eur(a['balance'])}, Kontor {fmt_eur(computed)}, "
                                  f"Differenz {fmt_eur(a['balance'] - computed)}",
                          "buchungen": jr.lines(ctx, year, acct, a["date"])})
    vst_open = [t for t in bank.transactions(ctx, year) if t["kind"] == "konto" and t["tax_key"] in ("19", "7")
                and t["amount"] < 0]
    if vst_open:
        found.append({"art": "vorsteuer_ohne_beleg", "konto": "1406", "betrag": len(vst_open),
                      "text": f"{len(vst_open)} Abgänge mit Vorsteuer ohne verknüpften Beleg: ohne Rechnung kein "
                              "Vorsteuerabzug (§ 15 UStG). Beleg beschaffen oder Steuerschlüssel 0",
                      "buchungen": [_as_line(t) for t in vst_open]})
    for t in bank.transactions(ctx, year):
        if t["kind"] == "beleg" and t["receipt_id"]:
            r = ctx.conn.execute("SELECT * FROM receipts WHERE id = ?", (t["receipt_id"],)).fetchone()
            has_vat = bool(r["tax"])
            if has_vat != (t["tax_key"] in ("19", "7")):
                found.append({"art": "steuer_beleg_abweichend", "konto": t["target_account"], "betrag": r["tax"] or 0,
                              "text": f"{t['booked_on']} {r['supplier']}: Beleg nennt {fmt_eur(r['tax'] or 0)} Vorsteuer, "
                                      f"Zuordnung sagt „{t['tax_key']}“ — welches stimmt? (Beiträge, Gebühren und Gerichts"
                                      "kosten sind nicht steuerbar)", "buchungen": [_as_line(t)]})
    for p in ctx.conn.execute(
            "SELECT p.*, d.number FROM payments p JOIN documents d ON d.id = p.document_id WHERE p.kind = 'einnahme' "
            "AND p.origin = 'ledger' AND p.source <> 'bank' AND substr(p.pay_date, 1, 4) = ?", (str(year),)):
        found.append({"art": "zahlung_ohne_bank", "konto": "1200", "betrag": p["amount"],
                      "text": f"{p['pay_date']} Zahlung {fmt_eur(p['amount'])} auf {p['number']} ohne Bankumsatz "
                              "(Bitcoin, Stripe, bar?) — Gegenkonto per Buchung von Hand", "buchungen": []})
    return found


def _as_line(t: dict) -> dict:
    return {"date": t["booked_on"], "account": t["bank_account"], "contra": "", "text": f"{t['counterparty']} {t['purpose']}",
            "beleg": f"B{t['id']}", "amount": t["amount"]}
