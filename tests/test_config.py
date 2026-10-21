from pathlib import Path

import pytest

from kontor import config


def test_no_default_data_dir_without_mandant(monkeypatch):
    monkeypatch.delenv("KONTOR_DATA", raising=False)
    with pytest.raises(config.ConfigError, match="Mandant"):
        config.data_dir()


def test_env_overrides_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("KONTOR_DATA", str(tmp_path))
    assert config.data_dir() == tmp_path.resolve()


def test_data_dir_inside_code_is_refused(monkeypatch):
    monkeypatch.setenv("KONTOR_DATA", str(config.CODE_ROOT / "daten"))
    with pytest.raises(config.ConfigError):
        config.data_dir()


def test_db_path():
    assert config.db_path(Path("/x")) == Path("/x/kontor.db")
