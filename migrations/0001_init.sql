-- ledger 0001: Grundschema. Migrationen werden nie nachträglich geändert.

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE customers (
  id INTEGER PRIMARY KEY,
  company TEXT NOT NULL DEFAULT '',
  contact TEXT NOT NULL DEFAULT '',
  street TEXT NOT NULL DEFAULT '',
  postcode TEXT NOT NULL DEFAULT '',
  city TEXT NOT NULL DEFAULT '',
  country TEXT NOT NULL DEFAULT 'DE',
  email TEXT NOT NULL DEFAULT '',
  vat_id TEXT NOT NULL DEFAULT '',
  leitweg_id TEXT NOT NULL DEFAULT '',
  note TEXT NOT NULL DEFAULT '',
  archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

CREATE TABLE catalog_items (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  unit TEXT NOT NULL DEFAULT 'psch.',
  unit_price INTEGER NOT NULL,
  tax_rate TEXT NOT NULL DEFAULT '19',
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE text_templates (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('angebot','rechnung')),
  slot TEXT NOT NULL CHECK (slot IN ('einleitung','schluss')),
  title TEXT NOT NULL,
  body TEXT NOT NULL,
  is_default INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE documents (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL CHECK (kind IN ('angebot','rechnung','abschlag','schluss','storno')),
  status TEXT NOT NULL DEFAULT 'entwurf'
    CHECK (status IN ('entwurf','abgeschlossen','angenommen','abgelehnt','teilbezahlt','bezahlt','storniert')),
  number TEXT UNIQUE,
  customer_id INTEGER REFERENCES customers(id),
  customer_json TEXT NOT NULL DEFAULT '{}',
  doc_date TEXT NOT NULL,
  service_date TEXT,
  service_from TEXT,
  service_to TEXT,
  due_date TEXT,
  valid_until TEXT,
  subject TEXT NOT NULL DEFAULT '',
  intro TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  conditions_json TEXT NOT NULL DEFAULT '[]',
  closing TEXT NOT NULL DEFAULT '',
  source_document_id INTEGER REFERENCES documents(id),
  net INTEGER NOT NULL DEFAULT 0,
  tax INTEGER NOT NULL DEFAULT 0,
  gross INTEGER NOT NULL DEFAULT 0,
  prepaid_gross INTEGER NOT NULL DEFAULT 0,
  prepaid_tax INTEGER NOT NULL DEFAULT 0,
  snapshot_json TEXT,
  pdf_path TEXT,
  sha256 TEXT,
  finalized_at TEXT,
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime')),
  updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

CREATE TABLE document_lines (
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  quantity TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT '',
  unit_price INTEGER NOT NULL,
  tax_rate TEXT NOT NULL,
  total INTEGER NOT NULL
);

CREATE TABLE document_allowances (
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
  position INTEGER NOT NULL,
  label TEXT NOT NULL,
  amount INTEGER NOT NULL,
  tax_rate TEXT NOT NULL
);

CREATE TABLE number_sequences (
  prefix TEXT NOT NULL,
  year INTEGER NOT NULL,
  last INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (prefix, year)
);

CREATE TABLE categories (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  euer_line TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE receipts (
  id INTEGER PRIMARY KEY,
  status TEXT NOT NULL DEFAULT 'eingang' CHECK (status IN ('eingang','geprueft')),
  receipt_date TEXT,
  supplier TEXT NOT NULL DEFAULT '',
  receipt_number TEXT NOT NULL DEFAULT '',
  net INTEGER,
  tax INTEGER,
  gross INTEGER,
  tax_kind TEXT CHECK (tax_kind IN ('inland_19','inland_7','ohne_vorsteuer','rc_eu','rc_drittland')),
  category_id INTEGER REFERENCES categories(id),
  file_path TEXT NOT NULL,
  original_name TEXT NOT NULL DEFAULT '',
  sha256 TEXT NOT NULL UNIQUE,
  origin TEXT NOT NULL CHECK (origin IN ('upload','telegram','cli')),
  parsed_json TEXT,
  paid_on TEXT,
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

CREATE TABLE bank_transactions (
  id INTEGER PRIMARY KEY,
  account TEXT NOT NULL,
  booked_on TEXT NOT NULL,
  amount INTEGER NOT NULL,
  counterparty TEXT NOT NULL DEFAULT '',
  purpose TEXT NOT NULL DEFAULT '',
  external_id TEXT NOT NULL UNIQUE,
  raw_json TEXT NOT NULL DEFAULT '{}',
  imported_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

CREATE TABLE payments (
  id INTEGER PRIMARY KEY,
  pay_date TEXT NOT NULL,
  amount INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('einnahme','ausgabe','ust')),
  document_id INTEGER REFERENCES documents(id),
  receipt_id INTEGER REFERENCES receipts(id),
  source TEXT NOT NULL DEFAULT 'manuell' CHECK (source IN ('manuell','bank')),
  bank_transaction_id INTEGER REFERENCES bank_transactions(id),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

CREATE TABLE payment_parts (
  payment_id INTEGER NOT NULL REFERENCES payments(id) ON DELETE CASCADE,
  tax_rate TEXT NOT NULL,
  net INTEGER NOT NULL,
  tax INTEGER NOT NULL
);

CREATE TABLE period_locks (
  month TEXT PRIMARY KEY,   -- JJJJ-MM
  locked_at TEXT NOT NULL,
  note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  object TEXT NOT NULL,
  object_id INTEGER,
  payload TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL
);

-- Abgeschlossene Dokumente: Inhalt unveränderbar, nur Status darf sich ändern (nie zurück auf Entwurf).
CREATE TRIGGER documents_locked_update BEFORE UPDATE ON documents
WHEN OLD.status <> 'entwurf' AND (
  NEW.status = 'entwurf'
  OR OLD.kind IS NOT NEW.kind OR OLD.number IS NOT NEW.number OR OLD.customer_id IS NOT NEW.customer_id
  OR OLD.customer_json IS NOT NEW.customer_json OR OLD.doc_date IS NOT NEW.doc_date
  OR OLD.service_date IS NOT NEW.service_date OR OLD.service_from IS NOT NEW.service_from
  OR OLD.service_to IS NOT NEW.service_to OR OLD.due_date IS NOT NEW.due_date
  OR OLD.valid_until IS NOT NEW.valid_until OR OLD.subject IS NOT NEW.subject OR OLD.intro IS NOT NEW.intro
  OR OLD.notes IS NOT NEW.notes OR OLD.conditions_json IS NOT NEW.conditions_json OR OLD.closing IS NOT NEW.closing
  OR OLD.source_document_id IS NOT NEW.source_document_id OR OLD.net IS NOT NEW.net OR OLD.tax IS NOT NEW.tax
  OR OLD.gross IS NOT NEW.gross OR OLD.prepaid_gross IS NOT NEW.prepaid_gross OR OLD.prepaid_tax IS NOT NEW.prepaid_tax
  OR OLD.snapshot_json IS NOT NEW.snapshot_json OR OLD.pdf_path IS NOT NEW.pdf_path
  OR OLD.sha256 IS NOT NEW.sha256 OR OLD.finalized_at IS NOT NEW.finalized_at)
BEGIN SELECT RAISE(ABORT, 'ledger: Dokument ist abgeschlossen und unveränderbar'); END;

CREATE TRIGGER documents_locked_delete BEFORE DELETE ON documents WHEN OLD.status <> 'entwurf'
BEGIN SELECT RAISE(ABORT, 'ledger: Abgeschlossene Dokumente werden nie gelöscht'); END;

CREATE TRIGGER lines_locked_insert BEFORE INSERT ON document_lines
WHEN (SELECT status FROM documents WHERE id = NEW.document_id) <> 'entwurf'
BEGIN SELECT RAISE(ABORT, 'ledger: Dokument ist abgeschlossen und unveränderbar'); END;
CREATE TRIGGER lines_locked_update BEFORE UPDATE ON document_lines
WHEN (SELECT status FROM documents WHERE id = OLD.document_id) <> 'entwurf'
BEGIN SELECT RAISE(ABORT, 'ledger: Dokument ist abgeschlossen und unveränderbar'); END;
CREATE TRIGGER lines_locked_delete BEFORE DELETE ON document_lines
WHEN (SELECT status FROM documents WHERE id = OLD.document_id) <> 'entwurf'
BEGIN SELECT RAISE(ABORT, 'ledger: Dokument ist abgeschlossen und unveränderbar'); END;

CREATE TRIGGER allowances_locked_insert BEFORE INSERT ON document_allowances
WHEN (SELECT status FROM documents WHERE id = NEW.document_id) <> 'entwurf'
BEGIN SELECT RAISE(ABORT, 'ledger: Dokument ist abgeschlossen und unveränderbar'); END;
CREATE TRIGGER allowances_locked_update BEFORE UPDATE ON document_allowances
WHEN (SELECT status FROM documents WHERE id = OLD.document_id) <> 'entwurf'
BEGIN SELECT RAISE(ABORT, 'ledger: Dokument ist abgeschlossen und unveränderbar'); END;
CREATE TRIGGER allowances_locked_delete BEFORE DELETE ON document_allowances
WHEN (SELECT status FROM documents WHERE id = OLD.document_id) <> 'entwurf'
BEGIN SELECT RAISE(ABORT, 'ledger: Dokument ist abgeschlossen und unveränderbar'); END;

-- Protokoll: nur anhängen.
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'ledger: Protokoll ist unveränderbar'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'ledger: Protokoll ist unveränderbar'); END;

-- Monatsabschluss: Zahlungen und Belege eines gesperrten Monats sind fest.
CREATE TRIGGER payments_lock_insert BEFORE INSERT ON payments
WHEN EXISTS (SELECT 1 FROM period_locks WHERE month = substr(NEW.pay_date, 1, 7))
BEGIN SELECT RAISE(ABORT, 'ledger: Der Monat ist abgeschlossen'); END;
CREATE TRIGGER payments_lock_update BEFORE UPDATE ON payments
WHEN EXISTS (SELECT 1 FROM period_locks WHERE month IN (substr(OLD.pay_date, 1, 7), substr(NEW.pay_date, 1, 7)))
BEGIN SELECT RAISE(ABORT, 'ledger: Der Monat ist abgeschlossen'); END;
CREATE TRIGGER payments_lock_delete BEFORE DELETE ON payments
WHEN EXISTS (SELECT 1 FROM period_locks WHERE month = substr(OLD.pay_date, 1, 7))
BEGIN SELECT RAISE(ABORT, 'ledger: Der Monat ist abgeschlossen'); END;

CREATE TRIGGER receipts_lock_update BEFORE UPDATE ON receipts
WHEN OLD.status = 'geprueft' AND EXISTS (SELECT 1 FROM period_locks
  WHERE month IN (substr(OLD.receipt_date, 1, 7), substr(NEW.receipt_date, 1, 7),
                  substr(IFNULL(OLD.paid_on, ''), 1, 7), substr(IFNULL(NEW.paid_on, ''), 1, 7)))
BEGIN SELECT RAISE(ABORT, 'ledger: Der Monat ist abgeschlossen'); END;
CREATE TRIGGER receipts_lock_confirm BEFORE UPDATE OF status ON receipts
WHEN NEW.status = 'geprueft' AND EXISTS (SELECT 1 FROM period_locks
  WHERE month IN (substr(NEW.receipt_date, 1, 7), substr(IFNULL(NEW.paid_on, ''), 1, 7)))
BEGIN SELECT RAISE(ABORT, 'ledger: Der Monat ist abgeschlossen'); END;
CREATE TRIGGER receipts_lock_delete BEFORE DELETE ON receipts
WHEN OLD.status = 'geprueft' AND EXISTS (SELECT 1 FROM period_locks
  WHERE month IN (substr(OLD.receipt_date, 1, 7), substr(IFNULL(OLD.paid_on, ''), 1, 7)))
BEGIN SELECT RAISE(ABORT, 'ledger: Der Monat ist abgeschlossen'); END;

-- Ausgabenkategorien (Zeilen der Anlage EÜR werden vor dem ersten Jahresabschluss eingetragen).
INSERT INTO categories (name) VALUES
  ('Fremdleistungen'), ('Software und Abos'), ('KI-Nutzung (API)'), ('Hardware (GWG)'),
  ('Telefon und Internet'), ('Fortbildung'), ('Reisekosten'), ('Bewirtung'), ('Werbung'),
  ('Rechts- und Steuerberatung'), ('Bürobedarf'), ('Kontoführung und Gebühren'),
  ('Arbeitszimmer'), ('Sonstiges');
