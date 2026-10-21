-- ledger 0008: Abo-Übersicht (nur Übersicht; gebucht wird weiter über die Belege).
CREATE TABLE subscriptions (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  provider TEXT NOT NULL DEFAULT '',
  amount INTEGER,                        -- Cent brutto je Intervall; NULL = noch nicht bekannt
  interval TEXT NOT NULL DEFAULT 'monat' CHECK (interval IN ('monat', 'jahr')),
  next_due TEXT,
  payment_via TEXT NOT NULL DEFAULT '',
  tax_kind TEXT CHECK (tax_kind IN ('inland_19', 'inland_7', 'ohne_vorsteuer', 'rc_eu', 'rc_drittland')),
  status TEXT NOT NULL DEFAULT 'aktiv' CHECK (status IN ('aktiv', 'gekuendigt')),
  ends_on TEXT,
  cancel_info TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);
