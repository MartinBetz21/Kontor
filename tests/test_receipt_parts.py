"""Belege mit mehreren Steuersätzen (Migration 0002) und Bewirtung (70/30, Anlass und Teilnehmer)."""
import sqlite3

import pytest

from kontor.core.errors import KontorError
from kontor.reports.euer import euer
from kontor.reports.ustva import ustva
from kontor.services import masterdata as md
from kontor.services import receipts as rc
from kontor.store.db import MIGRATIONS, migrate

JPG = b"\xff\xd8 bewirtung linde"
MEAL = [{"tax_kind": "inland_7", "gross": "32,10"}, {"tax_kind": "inland_19", "gross": "23,80"}]


def cat(ctx, name):
    return next(c["id"] for c in md.list_categories(ctx) if c["name"] == name)


def test_mixed_rates_receipt(ctx, ui):
    rid = rc.add_file(ctx, JPG, "foto.jpg", "telegram")["id"]
    rc.update(ctx, rid, {"receipt_date": "2026-09-24", "supplier": "Gasthaus Zur Linde", "parts": MEAL,
                         "category_id": cat(ctx, "Software und Abos"), "paid_on": "2026-09-24"})
    r = rc.confirm(ui, rid)
    assert (r["net"], r["tax"], r["gross"], r["tax_kind"]) == (5000, 590, 5590, None)
    assert [(p["tax_kind"], p["net"], p["tax"]) for p in rc.receipt_parts(ui, rid)] == \
        [("inland_7", 3000, 210), ("inland_19", 2000, 380)]
    assert ustva(ctx, "2026-09")["fields"]["66"]["tax"] == 590


def test_single_rate_still_works_and_becomes_one_part(ctx, ui):
    rid = rc.add_file(ctx, JPG, "foto.jpg", "upload")["id"]
    rc.update(ctx, rid, {"receipt_date": "2026-10-01", "supplier": "Hetzner", "gross": "11,90", "tax_kind": "inland_19",
                         "category_id": cat(ctx, "Software und Abos")})
    rc.confirm(ui, rid)
    assert [(p["tax_kind"], p["net"], p["tax"]) for p in rc.receipt_parts(ui, rid)] == [("inland_19", 1000, 190)]


def test_part_without_amount_refused(ctx):
    rid = rc.add_file(ctx, JPG, "foto.jpg", "upload")["id"]
    with pytest.raises(KontorError):
        rc.update(ctx, rid, {"parts": [{"tax_kind": "inland_7"}]})


def test_bewirtung_needs_occasion_and_splits_70_30(ctx, ui):
    rid = rc.add_file(ctx, JPG, "foto.jpg", "telegram")["id"]
    rc.update(ctx, rid, {"receipt_date": "2026-09-24", "supplier": "Gasthaus Zur Linde", "parts": MEAL,
                         "category_id": cat(ctx, "Bewirtung"), "paid_on": "2026-09-24"})
    with pytest.raises(KontorError, match="Anlass"):
        rc.confirm(ui, rid)
    rc.update(ui, rid, {"bewirtung_anlass": "Projektbesprechung", "bewirtung_teilnehmer": "Erika Mustermann, N. N."})
    rc.confirm(ui, rid)
    e = euer(ctx, 2026)
    rows = {a["category"]: a["net"] for a in e["ausgaben"]}
    assert rows == {"Bewirtung (70 % abziehbar)": 3500, "Bewirtung (30 % nicht abziehbar)": 1500}
    assert e["ausgaben_netto"] == 3500 and e["vorsteuer"] == 590


def test_migration_0002_from_0001_keeps_confirmed_receipt(tmp_path):
    path = tmp_path / "alt.db"
    conn = sqlite3.connect(path, isolation_level=None)
    conn.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    conn.executescript("BEGIN;\n" + (MIGRATIONS / "0001_init.sql").read_text(encoding="utf-8")
                       + "\nINSERT INTO schema_version VALUES (1);\nCOMMIT;")
    conn.execute("INSERT INTO receipts (status, receipt_date, supplier, net, tax, gross, tax_kind, file_path, sha256, "
                 "origin, paid_on) VALUES ('geprueft','2026-10-01','Hetzner',1000,190,1190,'inland_19','x','abc',"
                 "'upload','2026-10-01')")
    conn.row_factory = sqlite3.Row
    assert migrate(conn) >= 2
    parts = conn.execute("SELECT tax_kind, net, tax FROM receipt_parts").fetchall()
    assert [tuple(p) for p in parts] == [("inland_19", 1000, 190)]
