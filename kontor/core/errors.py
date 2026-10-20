class KontorError(Exception):
    """Fachlicher Fehler; die Meldung ist für den Menschen vor der Oberfläche geschrieben."""


class HumanOnly(KontorError):
    """Aktion ist dem Menschen in der Oberfläche vorbehalten."""
