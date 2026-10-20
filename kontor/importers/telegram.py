"""Belege aus dem Telegram-Thema „Belege“ holen. Ein eigener Bot (nicht Teil dieses Repos) legt sie auf einem Server
per SSH erreichbar ab (Datei + .json mit Originalnamen). Host und Ordner kommen aus --host, KONTOR_EINGANG_HOST /
KONTOR_EINGANG_ORDNER oder aus dem Register ("eingang": {"host": …, "ordner": …}); der Host ist ein Name aus ~/.ssh/config. Die .json schreibt der Bot erst nach dem Download: eine Datei ohne sie ist
noch unvollständig und wird übersprungen. Auf dem Server gelöscht wird erst nach Prüfsummen-Vergleich und Erfassung."""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
from pathlib import Path

from .. import config
from ..context import Ctx
from ..core.errors import KontorError
from ..services import receipts

REMOTE_DIR = "belege-eingang"
_STAMP = re.compile(r"^\d{8}-\d{6}-")
_NOTE = re.compile(r"-nachtrag-\d+\.json$")


def _fallback_name(name: str) -> str:
    return _STAMP.sub("", name)


class LocalTransport:
    """Ein Ordner auf diesem Rechner. Für Tests und als Ausweichweg."""

    def __init__(self, folder: Path):
        self.folder = Path(folder)

    def list(self) -> list[str]:
        if not self.folder.is_dir():
            return []
        names = {p.name for p in self.folder.iterdir() if p.is_file()}
        return sorted(n for n in names if not n.endswith(".json") and not n.startswith(".") and f"{n}.json" in names)

    def fetch(self, name: str) -> bytes:
        return (self.folder / name).read_bytes()

    def sha256(self, name: str) -> str:
        return hashlib.sha256(self.fetch(name)).hexdigest()

    def meta(self, name: str) -> dict:
        f = self.folder / f"{name}.json"
        return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}

    def list_notes(self) -> list[str]:
        if not self.folder.is_dir():
            return []
        return sorted(p.name for p in self.folder.iterdir() if p.is_file() and _NOTE.search(p.name))

    def read_note(self, name: str) -> dict:
        return json.loads((self.folder / name).read_text(encoding="utf-8"))

    def remove_note(self, name: str) -> None:
        (self.folder / name).unlink(missing_ok=True)

    def remove(self, name: str) -> None:
        (self.folder / name).unlink(missing_ok=True)
        (self.folder / f"{name}.json").unlink(missing_ok=True)


class SshTransport:
    def __init__(self, host: str | None = None, folder: str | None = None):
        conf = config.eingang()
        host = host or conf.get("host", "")
        if not host:
            raise KontorError("Kein Eingangs-Server: --host, KONTOR_EINGANG_HOST oder \"eingang\" im Register setzen.")
        self.host, self.folder = host, folder or conf.get("ordner") or REMOTE_DIR

    def _ssh(self, command: str, text: bool = True):
        return subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", self.host, command],
                              check=True, capture_output=True, text=text).stdout

    def _path(self, name: str) -> str:
        return shlex.quote(f"{self.folder}/{name}")

    def list(self) -> list[str]:
        out = self._ssh(f"mkdir -p {shlex.quote(self.folder)} && ls -1A {shlex.quote(self.folder)}")
        names = {n for n in out.splitlines() if n}
        return sorted(n for n in names if not n.endswith(".json") and not n.startswith(".") and f"{n}.json" in names)

    def fetch(self, name: str) -> bytes:
        return self._ssh(f"cat {self._path(name)}", text=False)

    def sha256(self, name: str) -> str:
        return self._ssh(f"sha256sum {self._path(name)}").split()[0]

    def meta(self, name: str) -> dict:
        try:
            return json.loads(self._ssh(f"cat {self._path(name + '.json')}"))
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return {}

    def list_notes(self) -> list[str]:
        out = self._ssh(f"mkdir -p {shlex.quote(self.folder)} && ls -1A {shlex.quote(self.folder)}")
        return sorted(n for n in out.splitlines() if _NOTE.search(n))

    def read_note(self, name: str) -> dict:
        return json.loads(self._ssh(f"cat {self._path(name)}"))

    def remove_note(self, name: str) -> None:
        self._ssh(f"rm -f {self._path(name)}")

    def remove(self, name: str) -> None:
        self._ssh(f"rm -f {self._path(name)} {self._path(name + '.json')}")


def pull(ctx: Ctx, transport) -> dict:
    """Erst die Belege (mit Begleittext als Notiz), dann die Nachträge (Text als Antwort aufs Foto)."""
    result = {"neu": [], "dubletten": [], "notizen": [], "fehler": []}
    for name in transport.list():
        try:
            content = transport.fetch(name)
            if hashlib.sha256(content).hexdigest() != transport.sha256(name):
                result["fehler"].append(f"{name}: Prüfsumme weicht ab, bleibt auf dem Server")
                continue
            meta = transport.meta(name)
            original = meta.get("originalname") or _fallback_name(name)
            ref = f"telegram:{meta['message_id']}" if meta.get("message_id") else None
            added = receipts.add_file(ctx, content, original, "telegram", note=meta.get("begleittext", ""),
                                      source_ref=ref)
            transport.remove(name)
            result["dubletten" if added["duplicate"] else "neu"].append(original)
        except (KontorError, subprocess.CalledProcessError, OSError) as exc:
            result["fehler"].append(f"{name}: {exc}")
    for name in transport.list_notes():
        try:
            note = transport.read_note(name)
            row = ctx.conn.execute("SELECT id, status, note FROM receipts WHERE source_ref = ?",
                                   (f"telegram:{note.get('reply_to')}",)).fetchone()
            if row is None:
                result["fehler"].append(f"Notiz „{note.get('text', '')[:60]}“: Foto dazu noch nicht da, bleibt liegen")
                continue
            if row["status"] != "eingang":
                result["fehler"].append(f"Notiz zu Beleg {row['id']} kam nach dem Bestätigen, bitte in der Oberfläche "
                                        f"ergänzen: {note.get('text', '')}")
            else:
                receipts.update(ctx, row["id"], {"note": "\n".join(filter(None, [row["note"], note.get("text", "")]))})
                result["notizen"].append(row["id"])
            transport.remove_note(name)
        except (KontorError, subprocess.CalledProcessError, OSError, json.JSONDecodeError) as exc:
            result["fehler"].append(f"{name}: {exc}")
    return result
