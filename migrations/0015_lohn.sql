-- Lohn (Profilschalter `lohn`, SPEC.md „Payroll“). Beträge in Cent, Monate als JJJJ-MM.
-- Ein Entwurf wird nicht gespeichert: Kontor rechnet ihn bei jeder Abfrage aus Stammdaten, Merkmalen und Bezügen neu.
-- Gespeichert wird erst die festgeschriebene Abrechnung, mit allem, was in die Rechnung einging (daten).
CREATE TABLE lohn_mitarbeiter (
  id INTEGER PRIMARY KEY,
  personalnr TEXT NOT NULL UNIQUE,
  vorname TEXT NOT NULL,
  nachname TEXT NOT NULL,
  geburtsdatum TEXT NOT NULL DEFAULT '',
  strasse TEXT NOT NULL DEFAULT '',
  plz TEXT NOT NULL DEFAULT '',
  ort TEXT NOT NULL DEFAULT '',
  land TEXT NOT NULL DEFAULT 'DE',
  iban TEXT NOT NULL DEFAULT '',
  steuer_id TEXT NOT NULL DEFAULT '',
  sv_nummer TEXT NOT NULL DEFAULT '',
  taetigkeit TEXT NOT NULL DEFAULT '',
  eintritt TEXT NOT NULL,
  austritt TEXT,
  notiz TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

-- Was sich im Lauf eines Beschäftigungsverhältnisses ändert (ELStAM, Krankenkasse, Beitragsgruppe), je ab Monat.
-- Es gilt die jüngste Fassung mit gueltig_ab <= Abrechnungsmonat.
CREATE TABLE lohn_merkmale (
  mitarbeiter_id INTEGER NOT NULL REFERENCES lohn_mitarbeiter(id),
  gueltig_ab TEXT NOT NULL,
  steuerklasse INTEGER NOT NULL DEFAULT 1 CHECK (steuerklasse BETWEEN 1 AND 6),
  faktor TEXT NOT NULL DEFAULT '',                 -- Faktorverfahren (Steuerklasse 4), z. B. 0,912
  kinderfreibetraege TEXT NOT NULL DEFAULT '0',    -- Zahl der Kinderfreibeträge, z. B. 1,5
  konfession TEXT NOT NULL DEFAULT '',             -- '' = keine Kirchensteuer, sonst ELStAM-Kürzel (ev, rk, …)
  konfession_ehegatte TEXT NOT NULL DEFAULT '',
  freibetrag INTEGER NOT NULL DEFAULT 0,           -- monatlicher Freibetrag laut ELStAM
  hinzurechnung INTEGER NOT NULL DEFAULT 0,        -- monatlicher Hinzurechnungsbetrag laut ELStAM
  pauschsteuer INTEGER NOT NULL DEFAULT 0,         -- Minijob mit einheitlicher Pauschsteuer statt ELStAM
  personengruppe TEXT NOT NULL DEFAULT '101',
  beitragsgruppe TEXT NOT NULL DEFAULT '1111' CHECK (length(beitragsgruppe) = 4),
  krankenkasse TEXT NOT NULL DEFAULT '',
  kv_zusatz TEXT NOT NULL DEFAULT '0',             -- Zusatzbeitrag der Kasse in Prozent
  u1 TEXT NOT NULL DEFAULT '0',                    -- Umlagesätze der Kasse in Prozent (0 = nicht umlagepflichtig)
  u2 TEXT NOT NULL DEFAULT '0',
  pv_kinder INTEGER NOT NULL DEFAULT 0,            -- Kinder unter 25 für den Beitragsabschlag
  pv_elterneigenschaft INTEGER NOT NULL DEFAULT 0, -- Elterneigenschaft nachgewiesen (kein Kinderlosenzuschlag)
  sachsen INTEGER NOT NULL DEFAULT 0,
  pkv_beitrag INTEGER NOT NULL DEFAULT 0,          -- privat versichert: Monatsbeitrag Basis-KV
  pkv_pv_beitrag INTEGER NOT NULL DEFAULT 0,       -- privat versichert: Monatsbeitrag Pflege-Pflichtversicherung
  rv_aufstockung INTEGER NOT NULL DEFAULT 1,       -- Minijob: AN stockt die Rentenversicherung auf
  aufwandskonto TEXT NOT NULL DEFAULT '',          -- leer = Einstellung lohn_konto_gehalt
  PRIMARY KEY (mitarbeiter_id, gueltig_ab)
);

-- Bezüge und Abzüge. Wiederkehrend: ab bis bis (bis leer = unbefristet); einmalig: ab = bis.
CREATE TABLE lohn_bezuege (
  id INTEGER PRIMARY KEY,
  mitarbeiter_id INTEGER NOT NULL REFERENCES lohn_mitarbeiter(id),
  lohnart TEXT NOT NULL CHECK (lohnart IN ('gehalt', 'einmalzahlung', 'sachbezug', 'steuerfrei', 'pauschal',
                                           'abzug')),
  bezeichnung TEXT NOT NULL DEFAULT '',
  betrag INTEGER NOT NULL,
  satz TEXT NOT NULL DEFAULT '',                   -- pauschal: Steuersatz in Prozent (15, 20, 25)
  konto TEXT NOT NULL DEFAULT '',                  -- leer = Konto aus den Einstellungen
  ab TEXT NOT NULL,
  bis TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

-- Werte des laufenden Jahres bis einschließlich bis_monat aus einem anderen Programm (Wechsel im Jahr).
CREATE TABLE lohn_vortrag (
  mitarbeiter_id INTEGER NOT NULL REFERENCES lohn_mitarbeiter(id),
  jahr INTEGER NOT NULL,
  bis_monat TEXT NOT NULL,
  daten TEXT NOT NULL,
  notiz TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (mitarbeiter_id, jahr)
);

-- Festgeschriebene Abrechnung. daten: Merkmale, Positionen, Eingaben und Ergebnisse der Rechenkerne, Summen.
-- Ändern geht nicht; eine Korrektur storniert (mit Grund) und schreibt den Monat neu fest.
CREATE TABLE lohn_abrechnungen (
  id INTEGER PRIMARY KEY,
  mitarbeiter_id INTEGER NOT NULL REFERENCES lohn_mitarbeiter(id),
  monat TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'fest' CHECK (status IN ('fest', 'storniert')),
  daten TEXT NOT NULL,
  brutto INTEGER NOT NULL,
  netto INTEGER NOT NULL,
  auszahlung INTEGER NOT NULL,
  ag_kosten INTEGER NOT NULL,
  pdf TEXT NOT NULL DEFAULT '',
  festgeschrieben_am TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
  storniert_am TEXT,
  storno_grund TEXT NOT NULL DEFAULT ''
);
CREATE UNIQUE INDEX lohn_abrechnung_je_monat ON lohn_abrechnungen (mitarbeiter_id, monat) WHERE status = 'fest';

CREATE TRIGGER lohn_abrechnung_fest BEFORE UPDATE ON lohn_abrechnungen
WHEN OLD.status = 'storniert' OR NEW.daten IS NOT OLD.daten OR NEW.brutto IS NOT OLD.brutto
  OR NEW.netto IS NOT OLD.netto OR NEW.auszahlung IS NOT OLD.auszahlung OR NEW.ag_kosten IS NOT OLD.ag_kosten
  OR NEW.monat IS NOT OLD.monat OR NEW.mitarbeiter_id IS NOT OLD.mitarbeiter_id
BEGIN SELECT RAISE(ABORT, 'ledger: Eine festgeschriebene Abrechnung ändert sich nicht (stornieren und neu festschreiben)'); END;
CREATE TRIGGER lohn_abrechnung_loeschen BEFORE DELETE ON lohn_abrechnungen
BEGIN SELECT RAISE(ABORT, 'ledger: Abrechnungen werden storniert, nicht gelöscht'); END;

-- Übermittlungen an ELSTER: Lohnsteuer-Anmeldung (je Zeitraum) und Lohnsteuerbescheinigung (je Arbeitnehmer und Jahr).
CREATE TABLE lohn_meldungen (
  id INTEGER PRIMARY KEY,
  art TEXT NOT NULL CHECK (art IN ('lsta', 'lstb')),
  jahr INTEGER NOT NULL,
  zeitraum TEXT NOT NULL DEFAULT '',
  mitarbeiter_id INTEGER REFERENCES lohn_mitarbeiter(id),
  test INTEGER NOT NULL,
  ok INTEGER NOT NULL,
  code INTEGER NOT NULL DEFAULT 0,
  transferticket TEXT NOT NULL DEFAULT '',
  werte TEXT NOT NULL DEFAULT '{}',
  datensatz TEXT NOT NULL DEFAULT '',
  protokoll TEXT NOT NULL DEFAULT '',
  am TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);
