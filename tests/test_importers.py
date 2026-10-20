import json

from kontor.importers.base import BankTransaction, store_transactions
from kontor.importers.telegram import LocalTransport, pull
from kontor.services import receipts as rc


def drop(folder, name, content, original):
    (folder / name).write_bytes(content)
    (folder / f"{name}.json").write_text(json.dumps({"datei": name, "originalname": original}), encoding="utf-8")


def test_pull_imports_removes_and_reports(ctx, tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    drop(server, "20261002-101500-foto-ab12.jpg", b"\xff\xd8 tankquittung", "foto-ab12.jpg")
    drop(server, "20261002-101600-rechnung.pdf", b"%PDF-1.4 hetzner", "rechnung.pdf")
    drop(server, "20261002-101700-notiz.txt", b"kein beleg", "notiz.txt")
    result = pull(ctx, LocalTransport(server))
    assert sorted(result["neu"]) == ["foto-ab12.jpg", "rechnung.pdf"]
    assert len(result["fehler"]) == 1 and "notiz.txt" in result["fehler"][0]
    assert sorted(p.name for p in server.iterdir()) == ["20261002-101700-notiz.txt", "20261002-101700-notiz.txt.json"]
    assert {r["origin"] for r in rc.list_receipts(ctx)} == {"telegram"}


def test_pull_twice_is_duplicate(ctx, tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    drop(server, "a.jpg", b"\xff\xd8 gleich", "a.jpg")
    pull(ctx, LocalTransport(server))
    drop(server, "b.jpg", b"\xff\xd8 gleich", "b.jpg")
    assert pull(ctx, LocalTransport(server))["dubletten"] == ["b.jpg"]
    assert not list(server.iterdir())


def test_bank_transactions_are_idempotent(ctx):
    t = BankTransaction("Testbank", "2026-10-05", 29900, "Alex Beispiel", "RE-2026-001", "tx-1", {"x": 1})
    assert store_transactions(ctx, [t, t]) == 1
    assert store_transactions(ctx, [t]) == 0


def test_pull_skips_files_without_sidecar(ctx, tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    (server / "halb.jpg").write_bytes(b"\xff\xd8 noch im Download")      # Bot schreibt .json erst danach
    assert pull(ctx, LocalTransport(server)) == {"neu": [], "dubletten": [], "notizen": [], "fehler": []}
    assert (server / "halb.jpg").exists()
