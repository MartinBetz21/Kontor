"""Erlaubte Statuswechsel nach Spec 6.1."""
from __future__ import annotations

from .errors import KontorError

INVOICE_KINDS = ("rechnung", "abschlag", "schluss")
ALL_KINDS = ("angebot",) + INVOICE_KINDS + ("storno",)
KIND_LABELS = {"angebot": "Angebot", "rechnung": "Rechnung", "abschlag": "Abschlagsrechnung",
               "schluss": "Schlussrechnung", "storno": "Stornorechnung"}
STATUS_LABELS = {"entwurf": "Entwurf", "abgeschlossen": "Abgeschlossen", "angenommen": "Angenommen",
                 "abgelehnt": "Abgelehnt", "teilbezahlt": "Teilbezahlt", "bezahlt": "Bezahlt",
                 "storniert": "Storniert"}

_INVOICE = {
    "entwurf": {"abgeschlossen"},
    "abgeschlossen": {"teilbezahlt", "bezahlt", "storniert"},
    "teilbezahlt": {"bezahlt", "storniert", "abgeschlossen"},
    "bezahlt": {"storniert", "teilbezahlt", "abgeschlossen"},
    "storniert": set(),
}
_TRANSITIONS = {
    "angebot": {"entwurf": {"abgeschlossen"}, "abgeschlossen": {"angenommen", "abgelehnt"},
                "abgelehnt": {"angenommen"}, "angenommen": set()},
    "rechnung": _INVOICE, "abschlag": _INVOICE, "schluss": _INVOICE,
    "storno": {"entwurf": {"abgeschlossen"}, "abgeschlossen": set()},
}


def check_transition(kind: str, old: str, new: str) -> None:
    if new not in _TRANSITIONS[kind].get(old, set()):
        raise KontorError(f"{KIND_LABELS[kind]}: Wechsel von „{STATUS_LABELS[old]}“ zu „{STATUS_LABELS[new]}“ "
                          f"ist nicht erlaubt.")


def payment_status(due: int, paid: int) -> str:
    if paid <= 0:
        return "abgeschlossen"
    return "bezahlt" if paid >= due else "teilbezahlt"
