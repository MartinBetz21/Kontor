-- Bank als Quelle der Buchungen (Spec 18.7a).
CREATE TABLE bank_accounts (
  iban TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  account TEXT NOT NULL,                     -- SKR04-Konto, z. B. 1813
  active INTEGER NOT NULL DEFAULT 1
);

-- Je Umsatz: Dublette (zählt nicht, bis bestätigt) und erkannte interne Umbuchung samt Gegenstück.
ALTER TABLE bank_transactions ADD COLUMN duplicate INTEGER NOT NULL DEFAULT 0;
ALTER TABLE bank_transactions ADD COLUMN internal INTEGER NOT NULL DEFAULT 0;
ALTER TABLE bank_transactions ADD COLUMN pair_id INTEGER REFERENCES bank_transactions(id);
ALTER TABLE bank_transactions ADD COLUMN counterparty_iban TEXT NOT NULL DEFAULT '';

-- Genau ein Ziel je Umsatz (Primärschlüssel).
CREATE TABLE bank_assignments (
  bank_transaction_id INTEGER PRIMARY KEY REFERENCES bank_transactions(id),
  kind TEXT NOT NULL CHECK (kind IN ('rechnung', 'konto', 'intern', 'beleg')),
  document_id INTEGER REFERENCES documents(id),
  payment_id INTEGER REFERENCES payments(id),
  receipt_id INTEGER REFERENCES receipts(id),
  account TEXT NOT NULL DEFAULT '',
  tax_key TEXT NOT NULL DEFAULT '' CHECK (tax_key IN ('', '19', '7', '0', 'rc_eu', 'rc_drittland')),
  note TEXT NOT NULL DEFAULT '',
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

-- Vorschläge je Gegenpartei: Muster (Großbuchstaben, Teilstring von Name + Zweck) → Konto und Steuerschlüssel.
CREATE TABLE bank_rules (
  id INTEGER PRIMARY KEY,
  pattern TEXT NOT NULL UNIQUE,
  account TEXT NOT NULL,
  tax_key TEXT NOT NULL DEFAULT '19' CHECK (tax_key IN ('19', '7', '0', 'rc_eu', 'rc_drittland')),
  source TEXT NOT NULL DEFAULT 'manuell',
  hits INTEGER NOT NULL DEFAULT 0
);

-- Endsaldo laut Kontoauszug (Spec 18.7).
CREATE TABLE bank_anchors (
  iban TEXT NOT NULL REFERENCES bank_accounts(iban),
  date TEXT NOT NULL,
  balance INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (iban, date)
);
