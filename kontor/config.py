"""Pfade, Mandanten und Instanz. Code und Daten bleiben getrennt: kein Datenverzeichnis darf im Code-Repo liegen.

Das Register (Spec 18.1) ist die Instanz-Datei einer Installation. Es nennt je Mandant id, Name, Datenordner und
Farbe, dazu optional Vorgaben für Einstellungen und Profilschalter, die in der Datenbank noch fehlen, und den
Telegram-Eingang. Alles, was eine einzelne Installation braucht, steht dort und nie im Code:

    {"basis": "..",                                   # optional: Wurzel für relative Datenpfade (relativ zur Datei)
     "eingang": {"host": "mein-server", "ordner": "belege-eingang"},
     "einstellungen": {...}, "profil": {...},          # optional: Vorgaben für alle Mandanten
     "mandanten": [{"id": "firma", "name": "Firma", "daten": "daten/firma", "farbe": "#2F4858",
                    "einstellungen": {...}, "profil": {...}}]}

Reihenfolge: Vorgabe im Code < Register oben < Register beim Mandanten < Wert in der Datenbank.
Ohne gewählten Mandanten öffnet nichts: eine Buchung im falschen Unternehmen ist der teuerste Fehler, den die
Software machen kann. KONTOR_DATA zeigt für Tests direkt auf einen Ordner und ersetzt dann das Register."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent          # Wurzel dieses Repos
LOCAL_ID = "lokal"
ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]*$")
REGISTER_KEYS = {"_hinweis", "_hint", "basis", "eingang", "einstellungen", "profil", "mandanten"}
ENTRY_KEYS = {"id", "name", "daten", "farbe", "einstellungen", "profil", "_hinweis"}


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Mandant:
    id: str
    name: str
    data: Path
    color: str
    settings: dict = field(default_factory=dict, compare=False)     # Vorgaben für Einstellungen
    profile: dict = field(default_factory=dict, compare=False)      # Vorgaben für Profilschalter


def _checked(path: Path) -> Path:
    path = path.expanduser().resolve()
    if path == CODE_ROOT or CODE_ROOT in path.parents:
        raise ConfigError(f"Datenordner darf nicht im Code-Verzeichnis liegen: {path}")
    return path


def data_dir() -> Path:
    """Nur KONTOR_DATA (Tests). Sonst gibt es keinen Standardordner, der Mandant muss gewählt sein."""
    raw = os.environ.get("KONTOR_DATA")
    if not raw:
        raise ConfigError("Kein Mandant gewählt: --mandant <id> oder KONTOR_MANDANT setzen.")
    return _checked(Path(raw))


def register_path() -> Path:
    """KONTOR_REGISTER, sonst ~/.config/kontor/mandanten.json (neben dem Zustand der Oberfläche)."""
    return Path(os.environ.get("KONTOR_REGISTER") or state_dir() / "mandanten.json")


_CACHE: dict[tuple[str, int], dict] = {}


def _register() -> tuple[Path, dict]:
    path = register_path()
    if not path.is_file():
        raise ConfigError(f"Mandantenregister fehlt: {path}")
    key = (str(path.resolve()), path.stat().st_mtime_ns)
    if key not in _CACHE:
        raw = json.loads(path.read_text(encoding="utf-8"))
        unknown = set(raw) - REGISTER_KEYS
        if unknown:
            raise ConfigError(f"Unbekannte Schlüssel im Register {path}: {', '.join(sorted(unknown))}")
        _CACHE.clear()
        _CACHE[key] = raw
    return path, _CACHE[key]


def mandanten() -> list[Mandant]:
    if os.environ.get("KONTOR_DATA"):
        return [Mandant(LOCAL_ID, "Lokal", data_dir(), "#555555")]
    path, raw = _register()
    base = Path(raw.get("basis") or ".").expanduser()
    base = base if base.is_absolute() else path.resolve().parent / base      # auch, wenn das Register ein Symlink ist
    result: list[Mandant] = []
    for entry in raw.get("mandanten", []):
        mid = str(entry.get("id", ""))
        if not ID_PATTERN.match(mid) or any(m.id == mid for m in result):
            raise ConfigError(f"Ungültige oder doppelte Mandanten-ID im Register: {mid!r}")
        unknown = set(entry) - ENTRY_KEYS
        if unknown:
            raise ConfigError(f"Unbekannte Schlüssel bei Mandant {mid}: {', '.join(sorted(unknown))}")
        data = Path(entry["daten"]).expanduser()                # relativ = relativ zu "basis" bzw. zum Register
        result.append(Mandant(mid, entry.get("name") or mid,
                              _checked(data if data.is_absolute() else base / data),
                              entry.get("farbe") or "#555555",
                              {**raw.get("einstellungen", {}), **entry.get("einstellungen", {})},
                              {**raw.get("profil", {}), **entry.get("profil", {})}))
    if not result:
        raise ConfigError(f"Mandantenregister ist leer: {path}")
    return result


def vorgaben(data: Path) -> tuple[dict, dict]:
    """Vorgaben (Einstellungen, Profil) aus dem Register für den Mandanten mit diesem Datenordner.
    Ohne Register, mit KONTOR_DATA oder für einen fremden Ordner: keine Vorgaben."""
    if os.environ.get("KONTOR_DATA") or not register_path().is_file():
        return {}, {}
    target = Path(data).expanduser().resolve()
    for m in mandanten():
        if m.data == target:
            return m.settings, m.profile
    return {}, {}


def eingang() -> dict:
    """Telegram-Eingang: Host und Ordner aus KONTOR_EINGANG_HOST / KONTOR_EINGANG_ORDNER, sonst aus dem Register."""
    conf: dict = {}
    if not os.environ.get("KONTOR_DATA") and register_path().is_file():
        conf = dict(_register()[1].get("eingang") or {})
    for key, env in (("host", "KONTOR_EINGANG_HOST"), ("ordner", "KONTOR_EINGANG_ORDNER")):
        if os.environ.get(env):
            conf[key] = os.environ[env]
    return conf


def mandant(mid: str | None) -> Mandant:
    all_ = mandanten()
    if os.environ.get("KONTOR_DATA"):
        return all_[0]
    if not mid:
        raise ConfigError("Kein Mandant gewählt: --mandant <id> oder KONTOR_MANDANT setzen. "
                          f"Mandanten: {', '.join(m.id for m in all_)}")
    for m in all_:
        if m.id == mid:
            return m
    raise ConfigError(f"Mandant {mid!r} steht nicht im Register. Mandanten: {', '.join(m.id for m in all_)}")


def state_dir() -> Path:
    """Zustand der Oberfläche (zuletzt benutzter Mandant). Liegt außerhalb der Datenordner."""
    return Path(os.environ.get("KONTOR_STATE") or Path.home() / ".config" / "kontor")


def last_used() -> str | None:
    try:
        return (state_dir() / "zuletzt").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def remember(mid: str) -> None:
    if last_used() == mid:
        return
    try:
        state_dir().mkdir(parents=True, exist_ok=True)
        (state_dir() / "zuletzt").write_text(mid, encoding="utf-8")
    except OSError:
        pass                                      # nur Bequemlichkeit; ohne Datei gilt der erste Mandant


# Nutzungsbedingungen (TERMS.md, DISCLAIMER.md): einmal bestätigt, gespeichert nur beim Nutzer (Zustandsordner).
TERMS_VERSION = "2026-10-20"


def terms_file() -> Path:
    return state_dir() / "bedingungen.json"


def terms_accepted() -> dict | None:
    try:
        raw = json.loads(terms_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if raw.get("version") == TERMS_VERSION else None


def accept_terms() -> dict:
    from datetime import datetime
    raw = {"version": TERMS_VERSION, "akzeptiert_am": datetime.now().isoformat(timespec="seconds"),
           "dateien": ["TERMS.md", "DISCLAIMER.md"]}
    terms_file().parent.mkdir(parents=True, exist_ok=True)
    terms_file().write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return raw


def db_path(data: Path) -> Path:
    return data / "kontor.db"
