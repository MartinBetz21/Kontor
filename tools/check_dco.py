"""DCO-Prüfung: jeder Commit zwischen BASE und HEAD braucht ein „Signed-off-by:“ mit der Adresse seines Autors
(Developer Certificate of Origin 1.1, https://developercertificate.org). Nur Standardbibliothek.

    python3 tools/check_dco.py <base-sha> <head-sha>
"""
from __future__ import annotations

import re
import subprocess
import sys


def main(base: str, head: str) -> int:
    log = subprocess.run(["git", "log", "--no-merges", "--format=%H%x00%ae%x00%B%x1e", f"{base}..{head}"],
                         capture_output=True, text=True, check=True).stdout
    fehlt = []
    for eintrag in filter(str.strip, log.split("\x1e")):
        sha, autor, text = eintrag.strip().split("\x00", 2)
        unterschriften = re.findall(r"^Signed-off-by: .+ <([^>]+)>\s*$", text, re.M)
        if autor.lower() not in (u.lower() for u in unterschriften):
            fehlt.append(f"{sha[:10]} ({autor}): kein Signed-off-by mit der Autor-Adresse")
    for f in fehlt:
        print(f"DCO fehlt  {f}")
    if fehlt:
        print("\nBitte jeden Commit mit `git commit -s` unterschreiben (CONTRIBUTING.md). Bestehende Commits: "
              "`git rebase --signoff <base>` und neu pushen.")
        return 1
    print("DCO: alle Commits unterschrieben")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
