"""Instanz: Vorgaben aus dem Register, Telegram-Eingang aus dem Register, Einrichtung mit `kontor init`.
Alle Namen erfunden."""
import json

import pytest

from kontor import config
from kontor.cli.main import main
from kontor.context import open_ctx
from kontor.importers.telegram import SshTransport
from kontor.services import masterdata as md


@pytest.fixture
def instanz(tmp_path, monkeypatch):
    monkeypatch.delenv("KONTOR_DATA", raising=False)
    monkeypatch.delenv("KONTOR_EINGANG_HOST", raising=False)
    monkeypatch.setenv("KONTOR_STATE", str(tmp_path / "zustand"))
    path = tmp_path / "instanz" / "kontor-instanz.json"
    path.parent.mkdir()
    path.write_text(json.dumps({
        "basis": "..",
        "eingang": {"host": "beleg-server", "ordner": "inbox"},
        "einstellungen": {"foreign_providers": "ACME CLOUD", "owner_names": "Erika Mustermann"},
        "mandanten": [
            {"id": "eins", "name": "Firma Eins", "daten": "daten/eins", "farbe": "#111111",
             "profil": {"telegram_eingang": "ja"}},
            {"id": "zwei", "name": "Firma Zwei GmbH", "daten": "daten/zwei", "farbe": "#222222",
             "einstellungen": {"foreign_providers": "OTHER LTD"}},
        ]}), encoding="utf-8")
    monkeypatch.setenv("KONTOR_REGISTER", str(path))
    return path


def test_relative_paths_resolve_against_basis(instanz, tmp_path):
    assert config.mandant("eins").data == (tmp_path / "daten" / "eins").resolve()


def test_register_as_symlink_resolves_paths_from_the_real_file(instanz, tmp_path, monkeypatch):
    link = tmp_path / "anderswo" / "mandanten.json"
    link.parent.mkdir()
    link.symlink_to(instanz)
    monkeypatch.setenv("KONTOR_REGISTER", str(link))
    assert config.mandant("eins").data == (tmp_path / "daten" / "eins").resolve()


def test_register_defaults_fill_settings_and_profile(instanz):
    eins = open_ctx("cli", config.mandant("eins").data)
    zwei = open_ctx("cli", config.mandant("zwei").data)
    assert md.get_settings(eins)["foreign_providers"] == "ACME CLOUD"           # oben im Register
    assert md.get_settings(zwei)["foreign_providers"] == "OTHER LTD"            # beim Mandanten gewinnt
    assert md.get_settings(zwei)["owner_names"] == "Erika Mustermann"
    assert md.has(eins, "telegram_eingang") and not md.has(zwei, "telegram_eingang")


def test_database_value_beats_register_default(instanz):
    ctx = open_ctx("cli", config.mandant("eins").data)
    md.set_settings(ctx, {"foreign_providers": "IN DER DB"})
    md.set_profile(ctx, {"telegram_eingang": "nein"})
    assert md.get_settings(ctx)["foreign_providers"] == "IN DER DB"
    assert not md.has(ctx, "telegram_eingang")


def test_unknown_keys_in_register_are_refused(instanz):
    raw = json.loads(instanz.read_text(encoding="utf-8"))
    raw["mandanten"][0]["einstellungen"] = {"gibtsnicht": "x"}
    instanz.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(config.ConfigError, match="gibtsnicht"):
        md.get_settings(open_ctx("cli", config.mandant("eins").data))
    raw["mandanten"][0]["tippfehler"] = 1
    instanz.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(config.ConfigError, match="tippfehler"):
        config.mandanten()


def test_without_register_there_are_no_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("KONTOR_DATA", str(tmp_path / "d"))
    ctx = open_ctx("cli")
    assert md.get_settings(ctx)["owner_names"] == "" and md.get_profile(ctx)["telegram_eingang"] == "nein"


def test_inbox_host_and_folder_come_from_register_or_environment(instanz, monkeypatch):
    t = SshTransport()
    assert (t.host, t.folder) == ("beleg-server", "inbox")
    monkeypatch.setenv("KONTOR_EINGANG_HOST", "anderer")
    assert SshTransport().host == "anderer" and SshTransport("cli-host").host == "cli-host"


def test_init_creates_data_folder_and_register_entry(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("KONTOR_DATA", raising=False)
    monkeypatch.setenv("KONTOR_REGISTER", str(tmp_path / "cfg" / "mandanten.json"))
    assert main(["init", "--daten", str(tmp_path / "buecher"), "--id", "neu", "--name", "Neu GmbH"]) == 1
    assert "TERMS.md" in json.loads(capsys.readouterr().err)["fehler"]                  # erst lesen, dann bestätigen
    assert not (tmp_path / "buecher").exists() and not config.terms_accepted()
    assert main(["init", "--daten", str(tmp_path / "buecher"), "--id", "neu", "--name", "Neu GmbH",
                 "--bedingungen-gelesen"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["bedingungen"]["version"] == config.TERMS_VERSION and config.terms_accepted()
    assert config.terms_file().parent == tmp_path / "zustand-test"                     # beim Nutzer, nicht im Code
    assert (tmp_path / "buecher" / "kontor.db").is_file() and out["mandant"] == "neu"
    assert "Name bzw. Firma" in out["einrichtung"]["fehlt"]
    assert [m.id for m in config.mandanten()] == ["neu"]
    assert main(["init", "--daten", str(tmp_path / "andere"), "--id", "neu"]) == 1           # ID schon vergeben
    assert "schon im Register" in json.loads(capsys.readouterr().err)["fehler"]


def test_init_refuses_a_data_folder_inside_the_code(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("KONTOR_DATA", raising=False)
    assert main(["init", "--daten", str(config.CODE_ROOT / "daten"), "--bedingungen-gelesen"]) == 1
    assert not (config.CODE_ROOT / "daten").exists()


def test_einrichtung_lists_what_is_missing_until_done(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("KONTOR_DATA", str(tmp_path / "d"))
    logo = tmp_path / "logo.svg"
    logo.write_text('<svg xmlns="http://www.w3.org/2000/svg"/>', encoding="utf-8")
    ctx = open_ctx("cli")
    md.set_profile(ctx, {"gewinnermittlung": "bilanz"})
    status = md.onboarding_status(ctx)
    assert not status["fertig"] and "Registergericht" in status["fehlt"]
    assert any(f.startswith("Voranmeldungszeitraum") for f in status["fehlt"])     # Standard „monat“ zählt nicht
    md.set_settings(ctx, {"name": "Beispiel GmbH", "street": "Weg 1", "postcode": "10115", "city": "Berlin",
                          "email": "info@example.com", "iban": "DE89370400440532013000", "tax_number_pending": "1",
                          "seat": "Berlin", "register_court": "Amtsgericht Berlin", "register_number": "HRB 1",
                          "managing_directors": "Erika Mustermann", "share_capital": "25000,00",
                          "ustva_period": "quartal"})
    ctx.conn.close()
    assert main(["einstellungen", "logo", str(logo)]) == 0
    capsys.readouterr()
    assert main(["einrichtung"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["fertig"] and out["fehlt"] == [] and out["einstellungen"]["logo_path"] == "logo.svg"
    assert (tmp_path / "d" / "logo.svg").is_file()
