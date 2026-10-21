"""Kleinpunkte aus dem Review vom 01.10.2026."""
import json
import socket
import subprocess
import sys
import time

import pytest

from kontor.core.allocation import allocate
from kontor.core.errors import KontorError
from kontor.core.tax_codes import complete_amounts, period_bounds
from kontor.services import documents as docs
from kontor.services import finalize as fin
from kontor.services import integrity
from kontor.services import masterdata as md
from kontor.services import receipts as rc
from tests.test_finalize import _schluss_setup, invoice, setup  # noqa: F401


def test_invalid_period_is_a_message():
    for spec in ("2026-13", "2026-00", "2026-Q5", "Oktober"):
        with pytest.raises(KontorError):
            period_bounds(spec)


def test_confirmed_receipt_moves_with_its_date(ctx, ui):
    rid = rc.add_file(ctx, b"\xff\xd8 umzug", "u.jpg", "upload")["id"]
    cat = md.list_categories(ctx)[0]["id"]
    rc.update(ctx, rid, {"receipt_date": "2026-10-01", "supplier": "X", "gross": "11,90", "tax_kind": "inland_19",
                         "category_id": cat})
    rc.confirm(ui, rid)
    rc.update(ui, rid, {"receipt_date": "2026-09-30"})
    r = rc.get_receipt(ui, rid)
    assert r["file_path"].startswith("belege/2026/09/") and (ui.data / r["file_path"]).exists()


def test_second_schluss_for_same_offer_refused(ui, setup):
    _, s = _schluss_setup(ui, setup)
    offer_id = docs.get_document(ui, s["id"])["source_document_id"]
    with pytest.raises(KontorError, match=s["number"]):
        docs.invoice_from_offer(ui, offer_id, "schluss")


def test_net_and_gross_without_tax_must_agree():
    assert complete_amounts("inland_19", 1000, None, 1190) == (1000, 190, 1190)
    with pytest.raises(KontorError):
        complete_amounts("inland_19", 1000, None, 1200)


def test_storno_names_the_original_buyer(ui, setup):
    d = fin.finalize(ui, invoice(ui, setup))
    md.update_customer(ui, setup, {"company": "Umbenannt GmbH"})
    st = fin.cancel(ui, d["id"])
    assert json.loads(st["snapshot_json"])["buyer"]["company"] == "Beispiel Technik GmbH & Co. KG"


def test_allocate_never_books_on_a_settled_rate():
    parts = allocate(10001, [("19", 20000, 3193), ("7", 0, 0)])
    assert [(p.rate, p.net + p.tax) for p in parts] == [("19", 10001)]


def test_cli_db_errors_are_json(tmp_path, monkeypatch, capsys):
    from kontor.cli.main import main
    monkeypatch.setenv("KONTOR_DATA", str(tmp_path / "d"))
    f = tmp_path / "b.jpg"
    f.write_bytes(b"\xff\xd8 x")
    main(["beleg", "erfassen", str(f)])
    capsys.readouterr()
    j = tmp_path / "v.json"
    j.write_text('{"category_id": 999}', encoding="utf-8")
    assert main(["beleg", "vorschlag", "1", "--json", str(j)]) == 1
    assert "Kategorie" in json.loads(capsys.readouterr().err)["fehler"]


def test_integrity_checks_receipt_files(ctx, ui):
    rid = rc.add_file(ctx, b"\xff\xd8 original", "o.jpg", "upload")["id"]
    assert integrity.check(ctx) == []
    (ctx.data / rc.get_receipt(ctx, rid)["file_path"]).write_bytes(b"veraendert")
    assert any("Beleg" in p for p in integrity.check(ctx))


def test_backdated_document_warns(ui, setup):
    fin.finalize(ui, invoice(ui, setup, doc_date="2026-10-10"))
    did = invoice(ui, setup, doc_date="2026-10-05")
    assert any("RE-2026-001" in w for w in fin.warnings(ui, did))


def test_app_migrates_once_at_start_not_per_request(tmp_path):
    from kontor.api.app import create_app
    from kontor.config import db_path
    create_app(tmp_path / "d")
    import sqlite3
    tables = {r[0] for r in sqlite3.connect(db_path(tmp_path / "d")).execute("SELECT name FROM sqlite_master")}
    assert "receipt_parts" in tables
    from kontor.context import open_ctx
    c = open_ctx("ui", tmp_path / "frisch", migrate=False)
    assert c.conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'documents'").fetchone()[0] == 0


def test_server_listens_on_localhost_only(tmp_path):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = {"KONTOR_DATA": str(tmp_path / "d"), "PATH": "/usr/bin:/bin"}
    proc = subprocess.Popen([sys.executable, "-m", "kontor.cli.main", "server", "--port", str(port)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            out = subprocess.run(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True).stdout
            if out:
                break
            time.sleep(0.1)
        assert f"127.0.0.1:{port}" in out and f"*:{port}" not in out
    finally:
        proc.terminate()
        proc.wait(timeout=10)
