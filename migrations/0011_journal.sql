-- Journal (Spec 18.8, 18.8a): eine Zeile = eine Bewegung auf einem Konto, Betrag in Cent, Soll positiv, Haben negativ.
-- art: eroeffnung (Vortrag aus der Saldenliste des Vorjahres), import (Lexware-Kontenexport, je Jahr ersetzbar),
--      manuell (Abschluss- und Korrekturbuchung mit Begründung), auto (aus Dokumenten und Bank, spätere Phase).
CREATE TABLE accounts (
  number TEXT PRIMARY KEY,
  name TEXT NOT NULL DEFAULT ''
);

CREATE TABLE journal (
  id INTEGER PRIMARY KEY,
  year INTEGER NOT NULL,
  date TEXT NOT NULL,
  account TEXT NOT NULL,
  amount INTEGER NOT NULL,
  contra TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '',
  beleg TEXT NOT NULL DEFAULT '',
  tax_rate TEXT NOT NULL DEFAULT '',
  tax_account TEXT NOT NULL DEFAULT '',
  art TEXT NOT NULL CHECK (art IN ('eroeffnung', 'import', 'manuell', 'auto')),
  ref TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
  CHECK (substr(date, 1, 4) = CAST(year AS TEXT))
);
CREATE INDEX journal_year_account ON journal (year, account);

-- Jahresabschluss aufgestellt: das Jahr ist fest (nur der Mensch, in der Oberfläche).
CREATE TABLE journal_locks (
  year INTEGER PRIMARY KEY,
  note TEXT NOT NULL,
  locked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

CREATE TRIGGER journal_lock_insert BEFORE INSERT ON journal
WHEN EXISTS (SELECT 1 FROM journal_locks WHERE year = NEW.year)
BEGIN SELECT RAISE(ABORT, 'ledger: Das Jahr ist gesperrt (Abschluss aufgestellt)'); END;
CREATE TRIGGER journal_lock_update BEFORE UPDATE ON journal
WHEN EXISTS (SELECT 1 FROM journal_locks WHERE year IN (OLD.year, NEW.year))
BEGIN SELECT RAISE(ABORT, 'ledger: Das Jahr ist gesperrt (Abschluss aufgestellt)'); END;
CREATE TRIGGER journal_lock_delete BEFORE DELETE ON journal
WHEN EXISTS (SELECT 1 FROM journal_locks WHERE year = OLD.year)
BEGIN SELECT RAISE(ABORT, 'ledger: Das Jahr ist gesperrt (Abschluss aufgestellt)'); END;
