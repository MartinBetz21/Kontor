"""Mac-Hülle: startet die Oberfläche (nur 127.0.0.1) als eigenen Prozess mit automatischem Neuladen, holt den
Telegram-Eingang und zeigt alles in einem eigenen Fenster. Codeänderungen laden den Server neu, die Seite merkt das
über /api/boot und aktualisiert sich selbst; ein Neustart der App ist dafür nicht nötig. Sie öffnet den zuletzt
benutzten Mandanten; die Titelleiste zeigt seinen Namen (Spec 18.2)."""
from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import webview  # noqa: E402

from kontor import config  # noqa: E402
from kontor.context import open_ctx  # noqa: E402
from kontor.services import masterdata as md  # noqa: E402
from kontor.importers.telegram import SshTransport, pull  # noqa: E402


class Api:
    """Für die Seite erreichbar als window.pywebview.api: nativer Ordner-Dialog für „PDF speichern unter …“."""

    def set_title(self, title: str):
        webview.windows[0].set_title(title)

    def choose_folder(self, start: str = ""):
        result = webview.windows[0].create_file_dialog(webview.FileDialog.FOLDER, directory=start or str(Path.home()))
        return result[0] if result else None


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fetch_inbox() -> None:
    """Telegram-Eingang nur für Mandanten mit dem Profilschalter telegram_eingang (Spec 6.7, 18.3)."""
    try:
        for m in config.mandanten():
            ctx = open_ctx("system", m.data)
            try:
                if md.has(ctx, "telegram_eingang"):
                    print(f"Eingang {m.id}: {pull(ctx, SshTransport())}", file=sys.stderr)
            finally:
                ctx.conn.close()
    except Exception as exc:  # offline oder Server weg: die App startet trotzdem
        print(f"Eingang nicht abgeholt: {exc}", file=sys.stderr)


def start_server(port: int) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "kontor.api.app:create_app", "--factory", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning", "--reload",
         "--reload-dir", str(ROOT / "kontor"), "--reload-dir", str(ROOT / "migrations"),
         "--reload-include", "*.py", "--reload-include", "*.sql"],
        cwd=ROOT)


def wait_until_up(port: int, seconds: float = 20) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/boot", timeout=1)
            return
        except OSError:
            time.sleep(0.2)


def main() -> None:
    fetch_inbox()
    port = free_port()
    server = start_server(port)
    try:
        wait_until_up(port)
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.create_window("Kontor", f"http://127.0.0.1:{port}/", width=1440, height=900,
                              maximized=True, min_size=(960, 640), js_api=Api())
        webview.start()
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()


if __name__ == "__main__":
    main()
