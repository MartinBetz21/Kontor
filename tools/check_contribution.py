"""Prüft einen Beitrag auf Daten und Geheimnisse, die nie ins Repo gehören. Nur Standardbibliothek.

    python3 tools/check_contribution.py --staged     # vor dem Commit (Hook .githooks/pre-commit)
    python3 tools/check_contribution.py --all        # ganzes Repo (CI auf jedem Pull Request)
    python3 tools/check_contribution.py DATEI ...    # einzelne Dateien

Exit 1, sobald etwas gefunden wird. Die Regeln sind allgemein: Zertifikate und Schlüssel, Datenbanken und
Datenordner, ELSTER-Werte (Hersteller-ID, PIN), echte IBANs und E-Mail-Adressen, Pfade aus dem eigenen Rechner,
Zugangsschlüssel, abgeschaltete Rückfragen für KI-Agenten. Erfundene Testwerte stehen unten in den Erlaubnislisten.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_GIT = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
ROOT = Path(_GIT.stdout.strip()) if _GIT.returncode == 0 else Path(__file__).resolve().parent.parent
IM_GIT = _GIT.returncode == 0
OHNE_GIT_AUSLASSEN = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules"}
SELF = "tools/check_contribution.py"

# Dateien, die nie ins Repo gehören (Name oder Pfad)
VERBOTENE_DATEIEN = [
    (r"\.(pfx|p12|pem|key|cer|crt|der|jks|keystore)$", "Zertifikat oder Schlüssel"),
    (r"(?i)(zertifikat|certificate)", "Datei mit Zertifikat im Namen"),
    (r"\.(db|db-journal|sqlite|sqlite3)$", "Datenbank"),
    (r"(^|/)\.env(\..*)?$", "Umgebungsdatei mit Zugangsdaten"),
    (r"(^|/)(archiv|belege|eingang|abgaben)/", "Datenordner eines Mandanten"),
    (r"(^|/)(mandanten|kontor-instanz)\.json$", "Instanz-Datei"),
    (r"(?i)(^|/)eric[^/]*\.log$|(^|/)ERiC-[\d.]+/", "ERiC-Protokoll oder ERiC-Bibliothek"),
    (r"\.log$", "Protokolldatei"),
    (r"\.jar$", "Java-Archiv (Mustang wird nicht mitgeliefert)"),
    (r"(^|/)\.claude/settings\.local\.json$", "lokale Claude-Einstellungen"),
    (r"(^|/)\.DS_Store$", "macOS-Systemdatei"),
]
BINAER_ERLAUBT = {"kontor/render/assets/fonts/archivo-400.ttf", "kontor/render/assets/fonts/archivo-600.ttf"}

# Erfundene Werte, die in Tests und Beispielen stehen dürfen
IBAN_ERLAUBT = {"DE89370400440532013000", "DE02120300000000202051", "DE47123456782222222222",
                "DE97123456783333333333", "DE21876543214444444444", "DE94123456781111111111"}
HERSTELLER_ERLAUBT = {"12345", "00000"}

_PRIV, _CERT = "PRIV" + "ATE KEY", "CERTI" + "FICATE"      # zusammengesetzt, damit diese Datei sich nicht selbst meldet
INHALT = [
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH |ENCRYPTED )?" + _PRIV + r"-----|-----BEGIN " + _CERT + r"-----"),
     "Schlüssel oder Zertifikat im Text"),
    (re.compile(r"(?i)\b(elster_)?pin\b[\"']?\s*[:=]\s*[\"']?\d{4,}"), "PIN"),
    (re.compile(r"sk-ant-[\w-]{10,}|sk-[A-Za-z0-9]{24,}|ghp_[A-Za-z0-9]{20,}|github_pat_\w{20,}|xox[bpas]-[\w-]{10,}|"
                r"AKIA[0-9A-Z]{16}|Bearer\s+[\w.-]{24,}"), "Zugangsschlüssel"),
    (re.compile(r"(?i)(api[_-]?key|token|secret|password|passwort)[\"']?\s*[:=]\s*[\"'](?!<)[\w./+-]{12,}[\"']"),
     "Zugangswert"),
    (re.compile("/" + "Users" + r"/(?!…|\.\.\.)[\w.-]+|/home/(?!runner\b)[a-z][\w.-]+|[A-Z]:\\" + "Users" + r"\\"),
     "Pfad aus einem eigenen Rechner"),
    (re.compile(r"--dangerously-" + r"skip-permissions|\bbypass" + r"Permissions\b|--permission-mode[ =](auto|bypass|acceptEdits)"),
     "Rückfragen für KI-Agenten abgeschaltet"),
    (re.compile(r"Bash\((\*|[^)]*(ssh|scp|rsync|curl|wget|python3?|node|bash|sh|zsh|osascript|open|eval|chmod|chown|cp|mv|rm|"
                r"git push)(?=[ :])[^)]*\*)\)"), "breite Freigabe für Befehle"),
]
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[0-9A-Z]{4}){3,7}(?: ?[0-9A-Z]{1,4})?\b")
MAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
MAIL_ERLAUBT = re.compile(r"@(example\.(com|org|net)|users\.noreply\.github\.com)$|^noreply@")
HERSTELLER = re.compile(r"<HerstellerID>(\d+)</HerstellerID>|elster_hersteller_id[\"']?\s*[:=]\s*[\"'](\d+)")


def dateiliste(modus: str, args: list[str]) -> list[tuple[str, bytes]]:
    if modus == "--staged":
        names = subprocess.run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"], cwd=ROOT,
                               capture_output=True, text=True).stdout.split("\0")
        return [(n, subprocess.run(["git", "show", f":{n}"], cwd=ROOT, capture_output=True).stdout) for n in names if n]
    if modus == "--all":
        if IM_GIT:
            names = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, text=True).stdout.split("\0")
        else:                                   # z. B. ein ZIP-Download: alle Dateien außer Werkzeug-Ordnern
            names = [str(f.relative_to(ROOT)) for f in ROOT.rglob("*")
                     if f.is_file() and not OHNE_GIT_AUSLASSEN & set(f.relative_to(ROOT).parts)
                     and not any(t.endswith(".egg-info") for t in f.relative_to(ROOT).parts)]
        return [(n, (ROOT / n).read_bytes()) for n in names if n and (ROOT / n).is_file()]
    return [(a, Path(a).read_bytes()) for a in args]


def pruefe(dateien: list[tuple[str, bytes]]) -> list[str]:
    funde = []
    for name, raw in dateien:
        for rx, was in VERBOTENE_DATEIEN:
            if re.search(rx, name):
                funde.append(f"{name}: {was}")
                break
        if raw[:2] == b"\x30\x82" and name not in BINAER_ERLAUBT:
            funde.append(f"{name}: sieht aus wie ein PKCS#12-Zertifikat")
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            if name not in BINAER_ERLAUBT:
                funde.append(f"{name}: Binärdatei (nur die Schriften in render/assets/fonts sind erlaubt)")
            continue
        if name == SELF or name.endswith("htmx.min.js"):
            continue
        for rx, was in INHALT:
            m = rx.search(text)
            if m:
                funde.append(f"{name}: {was} ({m.group()[:40]})")
        for m in IBAN.finditer(text):
            v = m.group().replace(" ", "")
            if re.fullmatch(r"[A-Z]{2}\d{2}[0-9A-Z]{11,30}", v) and v[:2] in ("DE", "AT", "CH", "NL", "FR", "BE", "LU", "GB", "IT", "ES") \
                    and v not in IBAN_ERLAUBT:
                funde.append(f"{name}: IBAN {m.group()} (nur erfundene Test-IBANs, siehe CONTRIBUTING.md)")
        for m in MAIL.finditer(text):
            if not MAIL_ERLAUBT.search(m.group()):
                funde.append(f"{name}: E-Mail-Adresse {m.group()} (nur @example.com/org/net)")
        for m in HERSTELLER.finditer(text):
            if (m.group(1) or m.group(2)) not in HERSTELLER_ERLAUBT:
                funde.append(f"{name}: ELSTER-Hersteller-ID {m.group(1) or m.group(2)}")
    return funde


def main() -> int:
    modus = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] in ("--staged", "--all") else "--files"
    funde = pruefe(dateiliste(modus, sys.argv[2:] if modus != "--files" else sys.argv[1:]))
    for f in funde:
        print(f"BLOCKIERT  {f}")
    if funde:
        print("\nDiese Inhalte gehören nicht ins Repo (CONTRIBUTING.md, „No real data“). Bitte entfernen und erneut committen.")
        return 1
    print("check_contribution: keine Funde")
    return 0


if __name__ == "__main__":
    sys.exit(main())
