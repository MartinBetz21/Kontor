-- Aufträge zur Freigabe: Ein Agent (oder der Mensch) bereitet eine Übermittlung an eine Behörde vor, der Mensch gibt
-- sie in der Oberfläche frei, einzeln oder gesammelt, mit einmal Zertifikats-PIN. werte hält fest, was beim Anlegen
-- gesendet würde; weicht es beim Senden ab, geht der Auftrag nicht raus (veraltet).
CREATE TABLE auftraege (
  id INTEGER PRIMARY KEY,
  art TEXT NOT NULL,
  parameter TEXT NOT NULL DEFAULT '{}',
  titel TEXT NOT NULL,
  erklaerung TEXT NOT NULL DEFAULT '',
  werte TEXT NOT NULL DEFAULT '{}',
  notiz TEXT NOT NULL DEFAULT '',
  test INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'offen' CHECK (status IN ('offen', 'gesendet', 'abgelehnt', 'veraltet', 'verworfen')),
  ergebnis TEXT NOT NULL DEFAULT '{}',
  erstellt_von TEXT NOT NULL,
  erstellt_am TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
  erledigt_am TEXT
);
