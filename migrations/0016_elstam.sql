-- ELStAM (Lohnsteuerabzugsmerkmale vom Bundeszentralamt für Steuern) über ELSTER, Profilschalter `lohn`.
-- Anfragen (An-, Ab-, Ummelden, Wechsel des Datenübermittlers, Abholung) und was zurückkommt.
CREATE TABLE lohn_elstam_vorgaenge (
  id INTEGER PRIMARY KEY,
  art TEXT NOT NULL CHECK (art IN ('anmelden', 'abmelden', 'ummelden', 'wechsel', 'abholen')),
  mitarbeiter_ids TEXT NOT NULL DEFAULT '[]',
  test INTEGER NOT NULL,
  ok INTEGER NOT NULL,
  code INTEGER NOT NULL DEFAULT 0,
  transferticket TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  datensatz TEXT NOT NULL DEFAULT '',
  am TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

-- Ein empfangener ELStAM-Datensatz je Arbeitnehmer und Gültigkeitsdatum (oder eine Ablehnung). Übernehmen schreibt
-- eine neue Fassung der Merkmale ab dem Monat von gueltig_ab; der Datensatz selbst bleibt, wie er kam.
CREATE TABLE lohn_elstam (
  id INTEGER PRIMARY KEY,
  vorgang_id INTEGER REFERENCES lohn_elstam_vorgaenge(id),
  mitarbeiter_id INTEGER REFERENCES lohn_mitarbeiter(id),
  idnr TEXT NOT NULL DEFAULT '',
  art TEXT NOT NULL,
  gueltig_ab TEXT NOT NULL DEFAULT '',
  daten TEXT NOT NULL,
  schluessel TEXT NOT NULL UNIQUE,
  empfangen_am TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
  uebernommen_am TEXT,
  verworfen_grund TEXT NOT NULL DEFAULT ''
);
