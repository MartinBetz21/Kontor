"""Mandanten (Spec 18.1–18.3): Register, Pflicht zur Wahl, getrennte Bücher, Mandant in der URL, Profil."""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kontor import config
from kontor.api.app import create_app
from kontor.cli.main import main
from kontor.context import open_ctx
from kontor.core.errors import KontorError
from kontor.services import masterdata as md

CODE = Path(__file__).resolve().parent.parent / "kontor"


@pytest.fixture
def register(tmp_path, monkeypatch):
    monkeypatch.delenv("KONTOR_DATA", raising=False)
    monkeypatch.delenv("KONTOR_MANDANT", raising=False)
    monkeypatch.setenv("KONTOR_STATE", str(tmp_path / "zustand"))
    path = tmp_path / "mandanten.json"
    path.write_text(json.dumps({"mandanten": [
        {"id": "eins", "name": "Firma Eins", "daten": str(tmp_path / "eins"), "farbe": "#B5651D"},
        {"id": "zwei", "name": "Firma Zwei GmbH", "daten": str(tmp_path / "zwei"), "farbe": "#1F4E79"},
    ]}), encoding="utf-8")
    monkeypatch.setenv("KONTOR_REGISTER", str(path))
    return path


@pytest.fixture
def run(capsys):
    def _run(*argv):
        code = main(list(argv))
        out, err = capsys.readouterr()
        return code, (json.loads(out) if out.strip() else None), (json.loads(err) if err.strip() else None)
    return _run


# -- Register ---------------------------------------------------------------
def test_register_lists_mandants(register):
    ids = [m.id for m in config.mandanten()]
    assert ids == ["eins", "zwei"]
    assert config.mandant("zwei").name == "Firma Zwei GmbH"


def test_relative_data_path_resolves_against_the_register(register):
    register.write_text(json.dumps({"mandanten": [{"id": "a", "name": "A", "daten": "daten/x", "farbe": "#000"}]}))
    assert config.mandant("a").data == (register.parent / "daten" / "x").resolve()


def test_no_mandant_is_an_error_that_names_the_choices(register):
    with pytest.raises(config.ConfigError, match="eins, zwei"):
        config.mandant(None)


def test_unknown_mandant_is_an_error(register):
    with pytest.raises(config.ConfigError, match="drei"):
        config.mandant("drei")


def test_mandant_data_inside_code_is_refused(register):
    register.write_text(json.dumps({"mandanten": [
        {"id": "a", "name": "A", "daten": str(config.CODE_ROOT / "daten"), "farbe": "#000"}]}))
    with pytest.raises(config.ConfigError):
        config.mandant("a")


def test_without_kontor_data_nothing_opens_by_default(register):
    with pytest.raises(config.ConfigError):
        open_ctx("cli")


# -- Kommandozeile ----------------------------------------------------------
def test_cli_refuses_every_command_without_mandant(register, run):
    for argv in (("kunde", "liste"), ("auswertung", "uebersicht")):
        code, out, err = run(*argv)
        assert code == 1 and out is None and "Mandant" in err["fehler"]


def test_cli_writes_only_into_the_chosen_mandant(register, run, tmp_path):
    f = tmp_path / "k.json"
    f.write_text(json.dumps({"company": "Kunde A"}), encoding="utf-8")
    code, out, _ = run("--mandant", "eins", "kunde", "anlegen", "--json", str(f))
    assert code == 0 and out["mandant"] == "eins" and out["company"] == "Kunde A"
    _, eins, _ = run("--mandant", "eins", "kunde", "liste")
    _, zwei, _ = run("--mandant", "zwei", "kunde", "liste")
    assert [k["company"] for k in eins["eintraege"]] == ["Kunde A"]
    assert zwei == {"mandant": "zwei", "eintraege": []}


def test_cli_takes_mandant_from_environment(register, run, monkeypatch):
    monkeypatch.setenv("KONTOR_MANDANT", "zwei")
    code, out, _ = run("kunde", "liste")
    assert code == 0 and out["mandant"] == "zwei"


def test_cli_lists_the_register_without_mandant(register, run):
    code, out, _ = run("mandanten")
    assert code == 0 and [m["id"] for m in out["eintraege"]] == ["eins", "zwei"]


def test_telegram_inbox_only_for_profiles_with_the_switch(register, run):
    ctx = open_ctx("system", config.mandant("zwei").data)
    md.set_profile(ctx, {"telegram_eingang": "nein"})
    ctx.conn.close()
    code, _, err = run("--mandant", "zwei", "eingang", "abholen")
    assert code == 1 and "Telegram" in err["fehler"]


# -- Profil -----------------------------------------------------------------
def test_profile_defaults_are_the_euer_profile(tmp_path):
    ctx = open_ctx("cli", tmp_path / "d")
    p = md.get_profile(ctx)
    assert (p["gewinnermittlung"], p["kontenrahmen"], p["nummernstellen"], p["telegram_eingang"]) == \
        ("euer", "kategorien", "3", "nein")


def test_profile_rejects_unknown_switches_and_values(tmp_path):
    ctx = open_ctx("cli", tmp_path / "d")
    with pytest.raises(KontorError):
        md.set_profile(ctx, {"gibtsnicht": "ja"})
    with pytest.raises(KontorError):
        md.set_profile(ctx, {"gewinnermittlung": "irgendwas"})
    md.set_profile(ctx, {"gewinnermittlung": "bilanz", "nummernstellen": "4"})
    assert md.get_profile(ctx)["gewinnermittlung"] == "bilanz"


def test_code_never_names_a_mandant():
    """Oberfläche, Kommandozeile und Auswertungen lesen Profilschalter, nie Mandanten-IDs (Spec 18.3)."""
    pattern = re.compile(r"""(mandant|\bm)(\.id|\[["']id["']\])\s*[!=]=\s*["']""", re.I)
    hits = [str(p.relative_to(CODE)) for p in CODE.rglob("*") if p.is_file() and p.suffix in (".py", ".html", ".js", ".css")
            and pattern.search(p.read_text(encoding="utf-8"))]
    assert hits == []


# -- Oberfläche -------------------------------------------------------------
@pytest.fixture
def web(register):
    app = create_app()
    with TestClient(app, base_url="http://127.0.0.1") as c:
        c.app_state = app.state
        yield c


def test_pages_live_under_the_mandant_and_show_its_name_and_color(web):
    r = web.get("/m/zwei/kunden")
    assert r.status_code == 200 and "Firma Zwei GmbH" in r.text and "#1F4E79" in r.text
    assert 'href="/m/zwei/kunden"' in r.text


def test_unknown_mandant_in_url_is_404(web):
    assert web.get("/m/drei/kunden").status_code == 404


def test_form_writes_into_the_mandant_of_its_url(web):
    r = web.post("/m/zwei/kunden", data={"company": "Nur in Zwei"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/m/zwei/kunden/")
    assert "Nur in Zwei" in web.get("/m/zwei/kunden").text
    assert "Nur in Zwei" not in web.get("/m/eins/kunden").text


def test_confirm_token_is_bound_to_the_mandant(web):
    tokens = web.app_state.confirm
    assert tokens["eins"] != tokens["zwei"]
    form = {"monat": "2026-09", "notiz": "VA abgegeben"}
    r = web.post("/m/eins/auswertungen/sperren", data={**form, "confirm": tokens["zwei"]}, follow_redirects=False)
    assert r.status_code == 403
    r = web.post("/m/eins/auswertungen/sperren", data={**form, "confirm": tokens["eins"]}, follow_redirects=False)
    assert r.status_code == 303


def test_root_redirects_to_last_used_mandant(web):
    assert web.get("/", follow_redirects=False).headers["location"] == "/m/eins/"
    web.get("/m/zwei/")
    assert web.get("/", follow_redirects=False).headers["location"] == "/m/zwei/"


# -- Nummern (6.2: fortlaufend über die Jahre) -------------------------------
from tests.test_finalize import invoice, setup  # noqa: E402,F401  (Fixture und Helfer)


def test_numbers_continue_across_years(ui, setup):
    from kontor.services import finalize as fin
    from kontor.services import integrity
    assert fin.finalize(ui, invoice(ui, setup, doc_date="2026-12-30"))["number"] == "RE-2026-001"
    assert fin.finalize(ui, invoice(ui, setup, doc_date="2027-01-04"))["number"] == "RE-2027-002"
    assert integrity.check(ui) == []


def test_number_width_comes_from_profile(ui, setup):
    from kontor.services import finalize as fin
    md.set_profile(ui, {"nummernstellen": "4"})
    assert fin.finalize(ui, invoice(ui, setup))["number"] == "RE-2026-0001"
