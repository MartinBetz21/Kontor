"""FastAPI-Fabrik. Der Server bindet nur an 127.0.0.1 (cli.main.HOST). Jede Anfrage bekommt einen eigenen
Ctx mit Akteur 'ui'. Unumkehrbare Aktionen verlangen zusätzlich den Bestätigungs-Token aus dem Formular."""
from __future__ import annotations

import contextvars
import hashlib
import hmac
import json
import re
import secrets
import subprocess
from pathlib import Path

from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .. import config
from ..config import Mandant
from ..context import open_ctx
from ..core.errors import KontorError
from ..core.money import fmt_eur, fmt_qty
from ..core.snapshot import date_de
from ..core.status import KIND_LABELS, STATUS_LABELS
from ..core.tax_codes import KZ_LABELS, TAX_KINDS, TAX_TREATMENTS
from ..services import masterdata as md

UI = Path(__file__).resolve().parent.parent / "ui"
templates = Jinja2Templates(directory=str(UI / "templates"))
templates.env.filters.update(
    eur=fmt_eur, qty=fmt_qty, date_de=date_de,
    money_input=lambda c: "" if c is None else fmt_eur(c).replace(" €", ""))
def _zip_catalog(titles, items):
    """Titel → Felder der Standardposition, für die Vorschläge im Editor."""
    return {i["title"]: {"description": i["description"], "price": fmt_eur(i["unit_price"]).replace(" €", ""),
                         "tax_rate": i["tax_rate"]} for i in items}


templates.env.filters.update(zip_catalog=_zip_catalog, fromjson=json.loads)
templates.env.globals.update(KIND_LABELS=KIND_LABELS, STATUS_LABELS=STATUS_LABELS, TAX_KINDS=TAX_KINDS,
                             TAX_TREATMENTS_OUT=TAX_TREATMENTS,
                             KZ_LABELS=KZ_LABELS)


# Präfix des Mandanten der laufenden Anfrage ("/m/<id>"); go() und die Vorlagen bauen Links damit.
PREFIX: contextvars.ContextVar[str] = contextvars.ContextVar("kontor_prefix", default="")
MANDANT_PATH = re.compile(r"^/m/([^/]+)(/.*)?$")
# DNS-Rebinding: eine fremde Domain, die auf 127.0.0.1 zeigt, ist für den Browser „same-origin“ und könnte alles
# lesen und posten. Deshalb nur Anfragen an diese Hostnamen (Port egal).
ALLOWED_HOSTS = ["127.0.0.1", "localhost"]
SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
# Kein Einbetten in fremde Seiten (Clickjacking auf „Abschließen“, „Löschen“), kein Raten des Dateityps bei Belegen.
SECURITY_HEADERS = {"X-Frame-Options": "SAMEORIGIN", "Content-Security-Policy": "frame-ancestors 'self'",
                    "X-Content-Type-Options": "nosniff"}


def render(request: Request, name: str, context: dict | None = None, status_code: int = 200):
    ctx = request.state.ctx
    m = request.state.mandant
    inbox = ctx.conn.execute("SELECT COUNT(*) FROM receipts WHERE status = 'eingang'").fetchone()[0]
    base = {"request": request, "confirm": request.app.state.confirm[m.id], "inbox_count": inbox,
            "hinweis": request.query_params.get("hinweis", ""), "M": f"/m/{m.id}", "mandant": m,
            "mandanten": list(request.app.state.mandanten.values()), "profil": md.get_profile(ctx)}
    return templates.TemplateResponse(request, name, {**base, **(context or {})}, status_code=status_code)


def _cross_site(request: Request) -> bool:
    """CSRF-Schutz: Formulare fremder Webseiten erreichen 127.0.0.1 sonst ungehindert. Browser schicken
    Sec-Fetch-Site und Origin; fehlen beide (Kommandozeile, Tests), ist es keine Browser-Anfrage von außen."""
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "none"):
        return True
    origin = request.headers.get("origin")
    return bool(origin) and urlparse(origin).netloc != request.url.netloc


def choose_folder_native(start: str) -> str | None:
    """Finder-Ordnerauswahl von macOS (AppleScript „choose folder“). None, wenn abgebrochen."""
    begin = start if start and Path(start).is_dir() else str(Path.home())
    script = ['on run argv', 'set p to choose folder with prompt "PDF speichern in …" '
              'default location (POSIX file (item 1 of argv))', 'return POSIX path of p', 'end run']
    args = ["osascript"] + [a for line in script for a in ("-e", line)] + [begin]
    result = subprocess.run(args, capture_output=True, text=True, timeout=600)
    path = result.stdout.strip()
    if result.returncode != 0:                  # abgebrochen
        return None
    return path.rstrip("/") or None


class MandantPrefix:
    """Alle Seiten liegen unter /m/<id>/… (Spec 18.2). Der Mandant steht in der URL, nicht im Serverzustand:
    zwei Tabs können zwei Mandanten zeigen, ein Formular schreibt immer in den Mandanten seiner URL."""

    def __init__(self, app, mandanten: dict[str, Mandant], remember: bool):
        self.app, self.mandanten, self.remember = app, mandanten, remember

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith("/static/") or scope["path"] == "/api/boot":
            return await self.app(scope, receive, send)
        match = MANDANT_PATH.match(scope["path"])
        if not match:
            if scope["path"] == "/":
                last = config.last_used()
                target = last if last in self.mandanten else next(iter(self.mandanten))
                return await RedirectResponse(f"/m/{target}/", status_code=303)(scope, receive, send)
            return await PlainTextResponse("Seite ohne Mandant: /m/<id>/…", status_code=404)(scope, receive, send)
        mid, rest = match.group(1), match.group(2) or "/"
        if mid not in self.mandanten:
            return await PlainTextResponse(f"Mandant {mid!r} gibt es nicht.", status_code=404)(scope, receive, send)
        if self.remember and scope["method"] == "GET":
            config.remember(mid)
        scope = {**scope, "path": rest, "raw_path": rest.encode(),
                 "state": {**scope.get("state", {}), "mandant": self.mandanten[mid]}}
        token = PREFIX.set(f"/m/{mid}")
        try:
            await self.app(scope, receive, send)
        finally:
            PREFIX.reset(token)


def create_app(data: Path | None = None) -> FastAPI:
    """Ohne `data` gilt das Mandantenregister; mit `data` (Tests) ein einzelner Mandant `lokal`."""
    app = FastAPI(title="Kontor", docs_url=None, redoc_url=None, openapi_url=None)
    mandanten = [Mandant(config.LOCAL_ID, "Lokal", data, "#555555")] if data else config.mandanten()
    app.state.mandanten = {m.id: m for m in mandanten}
    zufall = secrets.token_bytes(32)                # Bestätigungs-Token je Mandant (Spec 18.2)
    app.state.confirm = {m.id: hmac.new(zufall, m.id.encode(), hashlib.sha256).hexdigest()[:24] for m in mandanten}
    app.state.boot = secrets.token_hex(6)
    app.state.downloads = Path.home() / "Downloads"
    app.state.folder_chooser = choose_folder_native
    for m in mandanten:
        open_ctx("system", m.data).conn.close()   # Migrationen einmal beim Start, nicht bei jeder Anfrage
    app.mount("/static", StaticFiles(directory=str(UI / "static")), name="static")

    @app.middleware("http")
    async def ctx_per_request(request: Request, call_next):
        response = await _with_ctx(request, call_next)
        response.headers.update(SECURITY_HEADERS)
        return response

    async def _with_ctx(request: Request, call_next):
        if request.method not in SAFE_METHODS and _cross_site(request):
            return PlainTextResponse("Anfrage von einer fremden Seite abgelehnt.", status_code=403)
        if request.url.path.startswith("/static/") or request.url.path == "/api/boot":
            return await call_next(request)
        ctx = open_ctx("ui", request.state.mandant.data, migrate=False)
        request.state.ctx = ctx
        try:
            return await call_next(request)
        finally:
            ctx.conn.close()

    @app.exception_handler(KontorError)
    async def kontor_error(request: Request, exc: KontorError):
        return render(request, "error.html", {"message": str(exc)}, status_code=400)

    from .pages import router
    app.include_router(router)
    app.add_middleware(MandantPrefix, mandanten=app.state.mandanten, remember=data is None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)      # zuletzt = äußerste Schicht
    return app
