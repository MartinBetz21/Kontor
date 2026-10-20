-- ledger 0007: Ablageordner je Kunde (dorthin speichert „PDF speichern“, zuletzt gewählter Ordner wird gemerkt).
ALTER TABLE customers ADD COLUMN folder TEXT NOT NULL DEFAULT '';
