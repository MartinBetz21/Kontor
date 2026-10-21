import json

import pytest

from kontor.cli.main import HOST, main


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KONTOR_DATA", str(tmp_path / "daten"))

    def _run(*argv, data=None):
        if data is not None:
            f = tmp_path / "in.json"
            f.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            argv = (*argv, "--json", str(f))
        code = main(list(argv))
        out, err = capsys.readouterr()
        return code, (json.loads(out) if out.strip() else None), err
    return _run


def test_customer_and_invoice_draft(run, tmp_path):
    code, kunde, _ = run("kunde", "anlegen", data={"company": "Alex Beispiel", "contact": "Alex Beispiel"})
    assert code == 0 and kunde["id"] == 1
    code, d, _ = run("rechnung", "entwurf", data={"customer_id": 1, "service_date": "2026-09-18",
                                                 "lines": [{"title": "Aufbau Bestellportal", "quantity": "1",
                                                            "unit_price": "251,26"}]})
    assert code == 0 and (d["status"], d["gross"]) == ("entwurf", 29900)
    code, d2, _ = run("rechnung", "entwurf", "--id", str(d["id"]), data={"subject": "Pilotkunde"})
    assert d2["subject"] == "Pilotkunde" and d2["gross"] == 29900
    code, liste, _ = run("dokument", "liste", "--gruppe", "rechnungen")
    assert liste["mandant"] == "lokal" and [x["id"] for x in liste["eintraege"]] == [d["id"]]
    code, out, _ = run("dokument", "vorschau", str(d["id"]), "--out", str(tmp_path / "v.pdf"))
    assert code == 0 and (tmp_path / "v.pdf").read_bytes()[:4] == b"%PDF"


def test_errors_are_json_with_exit_1(run):
    code, out, err = run("kunde", "zeigen", "99")
    assert code == 1 and out is None and "gibt es nicht" in json.loads(err)["fehler"]


@pytest.mark.parametrize("argv", [
    ("dokument", "abschliessen", "1"), ("rechnung", "stornieren", "1"),
    ("beleg", "bestaetigen", "1"), ("beleg", "loeschen", "1"), ("monat", "sperren", "2026-10"),
])
def test_human_only_commands_do_not_exist(run, argv):
    with pytest.raises(SystemExit) as e:
        run(*argv)
    assert e.value.code == 2


def test_receipt_and_reports(run, tmp_path):
    f = tmp_path / "quittung.jpg"
    f.write_bytes(b"\xff\xd8 quittung")
    code, r, _ = run("beleg", "erfassen", str(f))
    assert code == 0 and r["duplicate"] is False
    code, r2, _ = run("beleg", "vorschlag", str(r["id"]), data={"supplier": "Hetzner", "gross": "11,90",
                                                               "tax_kind": "inland_19", "receipt_date": "2026-10-01"})
    assert r2["supplier"] == "Hetzner" and r2["status"] == "eingang"
    code, u, _ = run("auswertung", "ustva", "2026-10")
    assert code == 0 and u["fields"]["83"]["tax"] == 0
    code, ok, _ = run("pruefen")
    assert code == 0 and ok["ok"] is True


def test_server_binds_localhost_only():
    assert HOST == "127.0.0.1"


def test_cli_cannot_draft_storno(run):
    run("kunde", "anlegen", data={"company": "K"})
    code, out, err = run("rechnung", "entwurf", data={"kind": "storno", "customer_id": 1,
                                                     "lines": [{"title": "x", "quantity": "1", "unit_price": -500}]})
    assert code == 1 and "Storno" in json.loads(err)["fehler"]


def test_cli_pipeline_check(run):
    code, out, _ = run("pipeline", "pruefen")
    assert code == 0 and set(out) == {"mandant", "abgelaufen", "serienentwuerfe", "nachfassen"}
