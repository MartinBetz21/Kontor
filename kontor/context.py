"""Ctx bündelt Verbindung, Datenverzeichnis und Akteur ('ui' = der Mensch in der Oberfläche, 'cli' = ein KI-Agent wie Claude über die Kommandozeile, 'system')."""
from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from . import config
from .core.errors import HumanOnly
from .store.db import connect

ACTORS = ("ui", "cli", "system")


@dataclass
class Ctx:
    conn: sqlite3.Connection
    data: Path
    actor: str
    today: date | None = None

    def now_date(self) -> date:
        return self.today or date.today()


def open_ctx(actor: str, data: Path | None = None, migrate: bool = True) -> Ctx:
    if actor not in ACTORS:
        raise ValueError(actor)
    data = data or config.data_dir()
    for sub in ("archiv", "belege", "eingang"):
        (data / sub).mkdir(parents=True, exist_ok=True)
    if data.stat().st_mode & 0o077:
        os.chmod(data, 0o700)                   # Belege und PDFs darunter: andere Benutzer am Rechner sehen nichts
    return Ctx(connect(config.db_path(data), migrate), data, actor)


def require_human(ctx: Ctx) -> None:
    if ctx.actor != "ui":
        raise HumanOnly("Das macht nur ein Mensch in der Oberfläche.")
