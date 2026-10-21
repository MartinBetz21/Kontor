"""ERiC (ELSTER Rich Client) über ctypes (Spec 18.9). Die Bibliothek liegt wegen der Lizenz nicht im Code-Repo:
`KONTOR_ERIC` zeigt auf den Plattformordner, z. B. …/ERiC-43.5.4.0/Darwin-universal. ERiC ist prozessweit
einmal initialisierbar; jeder Aufruf initialisiert, arbeitet und beendet unter einer Sperre.

Prüfen läuft komplett lokal. Senden geht an ELSTER und braucht Zertifikat und PIN; die PIN wird nie gespeichert."""
from __future__ import annotations

import ctypes
import os
import re
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

from ..core.errors import KontorError

ERIC_VALIDIERE, ERIC_SENDE, ERIC_DRUCKE, ERIC_PRUEFE_HINWEISE = 1 << 1, 1 << 2, 1 << 5, 1 << 7
_LOCK = threading.Lock()


class _Druck(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("vorschau", ctypes.c_uint32), ("duplexDruck", ctypes.c_uint32),
                ("pdfName", ctypes.c_char_p), ("fussText", ctypes.c_char_p), ("pdfCallback", ctypes.c_void_p),
                ("pdfCallbackBenutzerdaten", ctypes.c_void_p)]


class _Krypto(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("zertifikatHandle", ctypes.c_uint32), ("pin", ctypes.c_char_p)]


def eric_dir() -> Path:
    raw = os.environ.get("KONTOR_ERIC", "")
    path = Path(raw) if raw else None
    if not path or not (path / "lib" / "libericapi.dylib").is_file() and not (path / "lib" / "libericapi.so").is_file():
        raise KontorError("ERiC nicht gefunden: KONTOR_ERIC auf den Plattformordner setzen "
                          "(…/ERiC-<version>/Darwin-universal).")
    return path


@contextmanager
def _eric():
    base = eric_dir()
    name = "libericapi.dylib" if (base / "lib" / "libericapi.dylib").is_file() else "libericapi.so"
    with _LOCK, tempfile.TemporaryDirectory(prefix="kontor-eric-") as log:
        lib = ctypes.CDLL(str(base / "lib" / name), mode=ctypes.RTLD_GLOBAL)   # RTLD_GLOBAL: Entwicklerhandbuch 4.2.3
        lib.EricRueckgabepufferErzeugen.restype = ctypes.c_void_p
        lib.EricRueckgabepufferInhalt.restype = ctypes.c_char_p
        lib.EricRueckgabepufferInhalt.argtypes = [ctypes.c_void_p]
        lib.EricRueckgabepufferFreigeben.argtypes = [ctypes.c_void_p]
        lib.EricHoleFehlerText.argtypes = [ctypes.c_int, ctypes.c_void_p]
        lib.EricBearbeiteVorgang.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint32, ctypes.c_void_p,
                                             ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        lib.EricMakeElsterStnr.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p]
        lib.EricGetHandleToCertificate.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32),
                                                   ctypes.c_char_p]
        lib.EricCloseHandleToCertificate.argtypes = [ctypes.c_uint32]
        rc = lib.EricInitialisiere(str(base / "lib" / "plugins").encode(), log.encode())
        if rc:
            raise KontorError(f"ERiC startet nicht (Code {rc}).")
        try:
            yield lib, Path(log)
        finally:
            lib.EricBeende()


def _text(lib, buf) -> str:
    return (lib.EricRueckgabepufferInhalt(buf) or b"").decode("utf-8", "replace")


def _fehlertext(lib, rc: int) -> str:
    buf = lib.EricRueckgabepufferErzeugen()
    try:
        lib.EricHoleFehlerText(rc, buf)
        return _text(lib, buf)
    finally:
        lib.EricRueckgabepufferFreigeben(buf)


def _meldungen(xml: str) -> list[str]:
    """Fehler und Hinweise aus der Rückgabe von EricBearbeiteVorgang, lesbar."""
    out = []
    for block in re.findall(r"<(?:FehlerRegelpruefung|Hinweis)>(.*?)</(?:FehlerRegelpruefung|Hinweis)>", xml, re.S):
        fields = dict(re.findall(r"<(\w+)>(.*?)</\1>", block, re.S))
        out.append(" · ".join(v for k, v in fields.items() if k in ("Feldidentifikator", "Text", "Code") and v))
    return out


def steuernummer13(steuernummer: str, bufa: str) -> str:
    """Steuernummer vom Bescheid (z. B. 214/1234/5670) ins 13-stellige ELSTER-Format; ERiC prüft sie dabei."""
    with _eric() as (lib, _):
        buf = lib.EricRueckgabepufferErzeugen()
        try:
            rc = lib.EricMakeElsterStnr(steuernummer.encode(), None, bufa.encode(), buf)
            if rc:
                raise KontorError(f"Steuernummer {steuernummer}: {_fehlertext(lib, rc)}")
            return _text(lib, buf)
        finally:
            lib.EricRueckgabepufferFreigeben(buf)


def _vorgang(lib, xml: str, datenart: str, flags: int, druck=None, krypto=None) -> dict:
    rb, sb = lib.EricRueckgabepufferErzeugen(), lib.EricRueckgabepufferErzeugen()
    try:
        rc = lib.EricBearbeiteVorgang(xml.encode("utf-8"), datenart.encode(), flags,
                                      ctypes.byref(druck) if druck else None, ctypes.byref(krypto) if krypto else None,
                                      None, rb, sb)
        rueckgabe, server = _text(lib, rb), _text(lib, sb)
        ticket = re.search(r"<TransferTicket>(.*?)</TransferTicket>", server) or \
            re.search(r"<Transferticket>(.*?)</Transferticket>", rueckgabe)
        meldungen = _meldungen(rueckgabe)
        # Ablehnungen des Annahmeservers stehen nur in der Serverantwort (z. B. 601015001: Hersteller-ID nicht zugelassen)
        for code, text in re.findall(r"<Rueckgabe><Code>(\d+)</Code><Text>(.*?)</Text></Rueckgabe>", server, re.S):
            if code != "0":
                meldungen.append(f"ELSTER {code} · {text.strip()}")
        return {"ok": rc == 0, "code": rc, "text": _fehlertext(lib, rc), "meldungen": meldungen,
                "rueckgabe": rueckgabe, "serverantwort": server, "transferticket": ticket.group(1) if ticket else ""}
    finally:
        lib.EricRueckgabepufferFreigeben(rb)
        lib.EricRueckgabepufferFreigeben(sb)


def pruefen(xml: str, datenart: str) -> dict:
    """Plausibilitätsprüfung ohne Versand, mit Hinweisen. Datenart mit Version, z. B. 'Bilanz_6.8'."""
    with _eric() as (lib, log):
        result = _vorgang(lib, xml, datenart, ERIC_VALIDIERE | ERIC_PRUEFE_HINWEISE)
        if not result["ok"] and (log / "eric.log").is_file():
            result["log"] = (log / "eric.log").read_text(encoding="utf-8", errors="replace")[-4000:]
        return result


def senden(xml: str, datenart: str, zertifikat: Path, pin: str, protokoll: Path) -> dict:
    """Prüfen und an ELSTER senden, authentifiziert mit Zertifikat. Das Übertragungsprotokoll landet als PDF unter
    `protokoll`. Nur aus der Oberfläche aufrufen (Spec 18.9: Senden ist eine menschliche Handlung)."""
    if not zertifikat.is_file():
        raise KontorError(f"Zertifikat nicht gefunden: {zertifikat}")
    with _eric() as (lib, log):
        handle, pin_support = ctypes.c_uint32(0), ctypes.c_uint32(0)
        rc = lib.EricGetHandleToCertificate(ctypes.byref(handle), ctypes.byref(pin_support), str(zertifikat).encode())
        if rc:
            raise KontorError(f"Zertifikat lässt sich nicht öffnen: {_fehlertext(lib, rc)}")
        try:
            druck = _Druck(4, 0, 0, str(protokoll).encode(), None, None, None)
            krypto = _Krypto(3, handle.value, pin.encode())
            result = _vorgang(lib, xml, datenart, ERIC_VALIDIERE | ERIC_SENDE | ERIC_DRUCKE, druck, krypto)
        finally:
            lib.EricCloseHandleToCertificate(handle.value)
        if not result["ok"] and (log / "eric.log").is_file():
            result["log"] = (log / "eric.log").read_text(encoding="utf-8", errors="replace")[-4000:]
        result["protokoll"] = str(protokoll) if protokoll.is_file() else ""
        return result
