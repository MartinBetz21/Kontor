"""Einstellungen, Kunden, Standardpositionen, Textvorlagen, Kategorien."""
from __future__ import annotations

from .. import config
from ..context import Ctx
from ..core.errors import KontorError
from ..core.money import parse_eur
from ..store import audit
from ..store.db import tx

SETTINGS_DEFAULTS = {
    "name": "", "trade_name": "", "street": "", "postcode": "", "city": "", "country": "DE",
    "email": "", "phone": "", "tax_number": "", "tax_number_pending": "", "vat_id": "", "iban": "", "bic": "", "bank_name": "",
    "ustva_period": "monat", "payment_days": "14", "offer_valid_days": "30", "logo_path": "",
    "payment_note_template": "Bitte überweisen Sie {betrag} bis {faellig_am} an {kontoinhaber}, IBAN {iban}, unter Angabe der Rechnungsnummer.",
    # Pflichtangaben einer Kapitalgesellschaft auf Geschäftsbriefen (§ 35a GmbHG), Spec 18.3
    "seat": "", "register_court": "", "register_number": "", "managing_directors": "",
    # Bilanz (Spec 18.8): Stammkapital und ausstehende Einlage; 2900 trägt nur die Differenz
    "share_capital": "", "share_capital_outstanding": "",
    # Anbieter ohne deutsche Umsatzsteuer (§ 13b): Vorsteuer auf sie ist eine Auffälligkeit
    "foreign_providers": "ANTHROPIC,CANVA,GOOGLE,LINKEDIN,NOTION,OPENAI,ZAPIER",
    # Eigene Namen im Feld Gegenpartei = interne Umbuchung; Jahre, deren Journal aus Lexware kommt
    "own_names": "", "lexware_journal_years": "",
    # Namen der Inhaber, wie die Bank sie bei Umbuchungen zwischen eigenen Konten zeigt (Auffälligkeit „Umbuchung
    # als Aufwand gebucht“). Leer = Prüfung aus.
    "owner_names": "",
    # ELSTER über ERiC (Spec 18.9): Bundesfinanzamtsnummer, Hersteller-ID (jede Person, die sendet, braucht eine eigene),
    # Plattformordner von ERiC, ELSTER-Zertifikat (.pfx) des Mandanten. Die PIN wird nie gespeichert.
    "finanzamt_nr": "", "elster_hersteller_id": "", "eric_pfad": "", "elster_zertifikat": "",
}
CUSTOMER_FIELDS = ("company", "contact", "street", "postcode", "city", "country", "email",
                   "vat_id", "leitweg_id", "note", "folder")
CATALOG_FIELDS = ("title", "description", "unit", "unit_price", "tax_rate", "active")
TAX_RATES = ("19", "7", "0")


def to_cents(value: int | str) -> int:
    if isinstance(value, bool):
        raise KontorError("Kein Betrag")
    return value if isinstance(value, int) else parse_eur(str(value))


# -- Einstellungen ----------------------------------------------------------
def _instance(ctx: Ctx) -> tuple[dict, dict]:
    """Vorgaben der Instanz (Register) für diesen Mandanten, geprüft gegen die bekannten Schlüssel."""
    settings, profile = config.vorgaben(ctx.data)
    unknown = (set(settings) - set(SETTINGS_DEFAULTS)) | {f"profil.{k}" for k in set(profile) - set(PROFILE_SWITCHES)}
    if unknown:
        raise config.ConfigError(f"Unbekannte Vorgaben im Register: {', '.join(sorted(unknown))}")
    return {k: str(v) for k, v in settings.items()}, {k: str(v) for k, v in profile.items()}


def get_settings(ctx: Ctx) -> dict[str, str]:
    """Wert in der Datenbank, sonst Vorgabe aus dem Register (Instanz), sonst Vorgabe im Code."""
    stored = {r["key"]: r["value"] for r in ctx.conn.execute("SELECT key, value FROM settings")}
    defaults = {**SETTINGS_DEFAULTS, **_instance(ctx)[0]}
    return {k: stored.get(k, v) for k, v in defaults.items()}


def set_settings(ctx: Ctx, values: dict[str, str]) -> None:
    unknown = set(values) - set(SETTINGS_DEFAULTS)
    if unknown:
        raise KontorError(f"Unbekannte Einstellung: {', '.join(sorted(unknown))}")
    clean = {k: str(v).strip() for k, v in values.items()}
    if "ustva_period" in clean and clean["ustva_period"] not in ("monat", "quartal", "jahr"):
        raise KontorError("Voranmeldungszeitraum ist 'monat', 'quartal' oder 'jahr'.")
    for key in ("payment_days", "offer_valid_days"):
        if key in clean and not clean[key].isdigit():
            raise KontorError(f"{key} muss eine Zahl von Tagen sein.")
    with tx(ctx.conn):
        for k, v in clean.items():
            ctx.conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (k, v))
        audit.log(ctx.conn, ctx.actor, "einstellungen", "settings", None, clean)



# -- Profil (Spec 18.3) -----------------------------------------------------
# Schalter je Mandant. Oberfläche, Kommandozeile und Auswertungen lesen nur diese Schalter, nie den Mandanten.
# Die Vorgaben sind das EÜR-Profil, damit bestehende Datenbanken unverändert weiterlaufen.
JA_NEIN = ("ja", "nein")
PROFILE_SWITCHES: dict[str, tuple[str, tuple[str, ...] | None]] = {
    "gewinnermittlung": ("euer", ("euer", "bilanz")),
    "besteuerung": ("ist", ("ist", "soll")),
    "kontenrahmen": ("kategorien", ("kategorien", "skr04")),
    "nummernstellen": ("3", ("3", "4", "5", "6")),
    "bankimport": ("", ("", "datev")),             # Format des Kontoumsatz-Exports, siehe importers/datev.py
    "zahlungsdienste": ("", None),                 # Liste, z. B. "stripe,pretix"
    "bitcoin": ("nein", JA_NEIN),
    "e_bilanz": ("nein", JA_NEIN),
    "ustva_eric": ("ja", JA_NEIN),
    "hinterlegung": ("nein", JA_NEIN),
    "telegram_eingang": ("nein", JA_NEIN),
}
PAYMENT_SERVICES = ("stripe", "pretix")


def get_profile(ctx: Ctx) -> dict[str, str]:
    stored = {r["key"][7:]: r["value"] for r in ctx.conn.execute("SELECT key, value FROM settings WHERE key LIKE 'profil_%'")}
    defaults = {**{k: d for k, (d, _) in PROFILE_SWITCHES.items()}, **_instance(ctx)[1]}
    return {k: stored.get(k, v) for k, v in defaults.items()}


def set_profile(ctx: Ctx, values: dict[str, str]) -> None:
    unknown = set(values) - set(PROFILE_SWITCHES)
    if unknown:
        raise KontorError(f"Unbekannter Profilschalter: {', '.join(sorted(unknown))}")
    clean = {k: str(v).strip() for k, v in values.items()}
    for k, v in clean.items():
        allowed = PROFILE_SWITCHES[k][1]
        if allowed is None:
            bad = [x for x in filter(None, v.split(",")) if x not in PAYMENT_SERVICES]
            if bad:
                raise KontorError(f"{k}: unbekannt {', '.join(bad)} (erlaubt: {', '.join(PAYMENT_SERVICES)})")
        elif v not in allowed:
            raise KontorError(f"{k}: {v!r} ist nicht erlaubt (erlaubt: {', '.join(a or '–' for a in allowed)})")
    with tx(ctx.conn):
        for k, v in clean.items():
            ctx.conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
                             "ON CONFLICT(key) DO UPDATE SET value = excluded.value", ("profil_" + k, v))
        audit.log(ctx.conn, ctx.actor, "profil", "settings", None, clean)


def has(ctx: Ctx, switch: str) -> bool:
    return get_profile(ctx)[switch] == "ja"


LOGO_SUFFIXES = (".svg", ".png", ".jpg", ".jpeg")


def set_logo(ctx: Ctx, content: bytes, filename: str) -> str:
    """Logo in den Datenordner legen (logo.<endung>); gibt den Wert für logo_path zurück."""
    suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if suffix not in LOGO_SUFFIXES:
        raise KontorError("Logo bitte als SVG, PNG oder JPG.")
    (ctx.data / f"logo{suffix}").write_bytes(content)
    return f"logo{suffix}"


# -- Einrichtung (Onboarding) -------------------------------------------------
# Was ein Mandant braucht, bevor die erste Rechnung abgeschlossen werden kann. Die KI-Einrichtung (ONBOARDING.md)
# fragt genau diese Punkte ab; `kontor einrichtung` zeigt, was noch fehlt.
REQUIRED_ALWAYS = (("name", "Name bzw. Firma"), ("street", "Straße"), ("postcode", "PLZ"), ("city", "Ort"),
                   ("email", "E-Mail"), ("iban", "IBAN für den Zahlungshinweis"))
REQUIRED_BILANZ = (("seat", "Sitz"), ("register_court", "Registergericht"), ("register_number", "Handelsregisternummer"),
                   ("managing_directors", "Geschäftsführer"), ("share_capital", "Stammkapital"))


def onboarding_status(ctx: Ctx) -> dict:
    s, p = get_settings(ctx), get_profile(ctx)
    missing = [label for key, label in REQUIRED_ALWAYS if not s[key]]
    stored = {r[0] for r in ctx.conn.execute("SELECT key FROM settings")}
    if "ustva_period" not in stored and "ustva_period" not in _instance(ctx)[0]:
        missing.append("Voranmeldungszeitraum (monat, quartal oder jahr; steht im Brief des Finanzamts)")
    if not (s["tax_number"] or s["tax_number_pending"]):
        missing.append("Steuernummer (oder „wird nachgeliefert“)")
    if p["gewinnermittlung"] == "bilanz":
        missing += [label for key, label in REQUIRED_BILANZ if not s[key]]
    hints = []
    if not s["logo_path"]:
        hints.append("kein Logo (optional)")
    if p["bankimport"]:
        n = ctx.conn.execute("SELECT COUNT(*) FROM bank_accounts").fetchone()[0]
        if not n:
            missing.append("mindestens ein Bankkonto (kontor bank konto …), weil der Bankimport an ist")
    if (p["e_bilanz"] == "ja") and not s["finanzamt_nr"]:
        hints.append("Bundesfinanzamtsnummer fehlt (nur für die E-Bilanz)")
    return {"fertig": not missing, "fehlt": missing, "hinweise": hints, "profil": p,
            "einstellungen": {k: v for k, v in s.items() if v}}


def set_counter_start(ctx: Ctx, prefix: str, last: int) -> None:
    """Letzte Nummer, die vor dem Kontor vergeben wurde (z. B. in Lexware). Geht nur, solange das Kontor in diesem
    Kreis noch keine eigene Nummer vergeben hat (Spec 18.3)."""
    if prefix not in ("RE", "AN") or int(last) < 0:
        raise KontorError("Kreis ist RE oder AN, die Nummer eine ganze Zahl ≥ 0.")
    row = ctx.conn.execute("SELECT last, start FROM number_counters WHERE prefix = ?", (prefix,)).fetchone()
    if row and row["last"] > row["start"]:
        raise KontorError(f"Im Kreis {prefix} sind schon Nummern vom Kontor vergeben (bis {row['last']}).")
    with tx(ctx.conn):
        ctx.conn.execute("INSERT INTO number_counters (prefix, last, start) VALUES (?, ?, ?) "
                         "ON CONFLICT(prefix) DO UPDATE SET last = excluded.last, start = excluded.start",
                         (prefix, int(last), int(last)))
        audit.log(ctx.conn, ctx.actor, "nummernkreis_start", "number_counters", None, {"kreis": prefix, "letzte": int(last)})

# -- Kunden -----------------------------------------------------------------
def _clean_customer(data: dict, base: dict | None = None) -> dict:
    unknown = set(data) - set(CUSTOMER_FIELDS) - {"id", "archived", "created_at"}
    if unknown:
        raise KontorError(f"Unbekanntes Kundenfeld: {', '.join(sorted(unknown))}")
    merged = {f: (base or {}).get(f, "") for f in CUSTOMER_FIELDS}
    merged.update({k: str(v).strip() for k, v in data.items() if k in CUSTOMER_FIELDS})
    merged["country"] = (merged["country"] or "DE").upper()
    if not merged["company"] and not merged["contact"]:
        raise KontorError("Firma oder Ansprechpartner fehlt.")
    return merged


def create_customer(ctx: Ctx, data: dict) -> int:
    c = _clean_customer(data)
    with tx(ctx.conn):
        cur = ctx.conn.execute(
            f"INSERT INTO customers ({', '.join(CUSTOMER_FIELDS)}) VALUES ({', '.join('?' * len(CUSTOMER_FIELDS))})",
            [c[f] for f in CUSTOMER_FIELDS])
        audit.log(ctx.conn, ctx.actor, "anlegen", "kunde", cur.lastrowid, c)
    return cur.lastrowid


def update_customer(ctx: Ctx, cid: int, data: dict) -> None:
    c = _clean_customer(data, get_customer(ctx, cid))
    with tx(ctx.conn):
        ctx.conn.execute(f"UPDATE customers SET {', '.join(f + ' = ?' for f in CUSTOMER_FIELDS)} WHERE id = ?",
                         [c[f] for f in CUSTOMER_FIELDS] + [cid])
        if "archived" in data:
            ctx.conn.execute("UPDATE customers SET archived = ? WHERE id = ?", (1 if data["archived"] else 0, cid))
        audit.log(ctx.conn, ctx.actor, "aendern", "kunde", cid, data)


def get_customer(ctx: Ctx, cid: int) -> dict:
    row = ctx.conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone()
    if row is None:
        raise KontorError(f"Kunde {cid} gibt es nicht.")
    return dict(row)


def list_customers(ctx: Ctx, include_archived: bool = False) -> list[dict]:
    sql = "SELECT * FROM customers" + ("" if include_archived else " WHERE archived = 0")
    return [dict(r) for r in ctx.conn.execute(sql + " ORDER BY company, contact")]


def customer_label(c: dict) -> str:
    return c.get("company") or c.get("contact") or "—"


# -- Standardpositionen -----------------------------------------------------
def _clean_item(data: dict, base: dict | None = None) -> dict:
    item = {"title": "", "description": "", "unit": "psch.", "unit_price": 0, "tax_rate": "19", "active": 1}
    item.update({k: base[k] for k in CATALOG_FIELDS if base and k in base})
    for k, v in data.items():
        if k not in CATALOG_FIELDS:
            continue
        item[k] = to_cents(v) if k == "unit_price" else (int(bool(v)) if k == "active" else str(v).strip())
    if not item["title"]:
        raise KontorError("Bezeichnung fehlt.")
    if item["tax_rate"] not in TAX_RATES:
        raise KontorError(f"Steuersatz {item['tax_rate']} gibt es nicht (19, 7 oder 0).")
    return item


def create_catalog_item(ctx: Ctx, data: dict) -> int:
    i = _clean_item(data)
    with tx(ctx.conn):
        cur = ctx.conn.execute(
            "INSERT INTO catalog_items (title, description, unit, unit_price, tax_rate, active, sort_order) "
            "VALUES (?,?,?,?,?,?, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM catalog_items))",
            [i[k] for k in CATALOG_FIELDS])
        audit.log(ctx.conn, ctx.actor, "anlegen", "position", cur.lastrowid, i)
    return cur.lastrowid


def update_catalog_item(ctx: Ctx, iid: int, data: dict) -> None:
    i = _clean_item(data, get_catalog_item(ctx, iid))
    with tx(ctx.conn):
        ctx.conn.execute("UPDATE catalog_items SET title=?, description=?, unit=?, unit_price=?, tax_rate=?, active=? "
                         "WHERE id=?", [i[k] for k in CATALOG_FIELDS] + [iid])
        audit.log(ctx.conn, ctx.actor, "aendern", "position", iid, i)


def get_catalog_item(ctx: Ctx, iid: int) -> dict:
    row = ctx.conn.execute("SELECT * FROM catalog_items WHERE id = ?", (iid,)).fetchone()
    if row is None:
        raise KontorError(f"Standardposition {iid} gibt es nicht.")
    return dict(row)


def list_catalog(ctx: Ctx, query: str = "", include_inactive: bool = False) -> list[dict]:
    sql, args = "SELECT * FROM catalog_items WHERE 1=1", []
    if not include_inactive:
        sql += " AND active = 1"
    if query:
        sql += " AND (title LIKE ? OR description LIKE ?)"
        args += [f"%{query}%", f"%{query}%"]
    return [dict(r) for r in ctx.conn.execute(sql + " ORDER BY sort_order, title", args)]


def reorder_catalog(ctx: Ctx, ids: list[int]) -> None:
    """Reihenfolge der Standardpositionen (Drag & Drop): Position in der Liste = Sortierung."""
    with tx(ctx.conn):
        for n, iid in enumerate(ids, 1):
            ctx.conn.execute("UPDATE catalog_items SET sort_order = ? WHERE id = ?", (n, int(iid)))
        audit.log(ctx.conn, ctx.actor, "reihenfolge", "position", None, {"ids": [int(i) for i in ids]})


# -- Textvorlagen -----------------------------------------------------------
def _template_kind(kind: str) -> str:
    return "angebot" if kind == "angebot" else "rechnung"


def create_template(ctx: Ctx, data: dict) -> int:
    kind, slot = data.get("kind"), data.get("slot")
    if kind not in ("angebot", "rechnung") or slot not in ("einleitung", "schluss"):
        raise KontorError("Vorlage braucht kind (angebot/rechnung) und slot (einleitung/schluss).")
    if not str(data.get("body", "")).strip():
        raise KontorError("Vorlagentext fehlt.")
    with tx(ctx.conn):
        if data.get("is_default"):
            ctx.conn.execute("UPDATE text_templates SET is_default = 0 WHERE kind = ? AND slot = ?", (kind, slot))
        cur = ctx.conn.execute("INSERT INTO text_templates (kind, slot, title, body, is_default) VALUES (?,?,?,?,?)",
                               (kind, slot, data.get("title") or slot, data["body"].strip(),
                                1 if data.get("is_default") else 0))
        audit.log(ctx.conn, ctx.actor, "anlegen", "textvorlage", cur.lastrowid, data)
    return cur.lastrowid


def update_template(ctx: Ctx, tid: int, data: dict) -> None:
    row = ctx.conn.execute("SELECT * FROM text_templates WHERE id = ?", (tid,)).fetchone()
    if row is None:
        raise KontorError(f"Textvorlage {tid} gibt es nicht.")
    merged = {**dict(row), **data}
    with tx(ctx.conn):
        if merged.get("is_default"):
            ctx.conn.execute("UPDATE text_templates SET is_default = 0 WHERE kind = ? AND slot = ?",
                             (merged["kind"], merged["slot"]))
        ctx.conn.execute("UPDATE text_templates SET title=?, body=?, is_default=? WHERE id=?",
                         (merged["title"], merged["body"], 1 if merged.get("is_default") else 0, tid))
        audit.log(ctx.conn, ctx.actor, "aendern", "textvorlage", tid, data)


def list_templates(ctx: Ctx, kind: str | None = None) -> list[dict]:
    if kind:
        rows = ctx.conn.execute("SELECT * FROM text_templates WHERE kind = ? ORDER BY slot, title",
                                (_template_kind(kind),))
    else:
        rows = ctx.conn.execute("SELECT * FROM text_templates ORDER BY kind, slot, title")
    return [dict(r) for r in rows]


def default_text(ctx: Ctx, kind: str, slot: str) -> str:
    row = ctx.conn.execute("SELECT body FROM text_templates WHERE kind = ? AND slot = ? AND is_default = 1",
                           (_template_kind(kind), slot)).fetchone()
    return row["body"] if row else ""


# -- Kategorien -------------------------------------------------------------
def list_categories(ctx: Ctx) -> list[dict]:
    return [dict(r) for r in ctx.conn.execute("SELECT * FROM categories WHERE active = 1 ORDER BY name")]
