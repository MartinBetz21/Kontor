from datetime import date

import pytest

from kontor.context import Ctx, open_ctx


@pytest.fixture(autouse=True)
def eigener_zustand(tmp_path, monkeypatch):
    """Kein Test schreibt in ~/.config/kontor (zuletzt benutzter Mandant, bestätigte Bedingungen)."""
    monkeypatch.setenv("KONTOR_STATE", str(tmp_path / "zustand-test"))


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setenv("KONTOR_DATA", str(tmp_path / "daten"))
    c = open_ctx("cli")
    c.today = date(2026, 10, 2)
    yield c
    c.conn.close()


@pytest.fixture
def ui(ctx):
    """Dieselbe Datenbank, aber als Mensch in der Oberfläche."""
    return Ctx(ctx.conn, ctx.data, "ui", ctx.today)
