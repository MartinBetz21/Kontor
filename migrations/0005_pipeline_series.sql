-- ledger 0005: Zahlungsmodus je Angebot, Nachfass-Aufgaben (z. B. abgelaufenes Angebot), Serienrechnungen,
-- Reihenfolge der Standardpositionen.

ALTER TABLE documents ADD COLUMN payment_mode TEXT CHECK (payment_mode IN ('vorab', 'danach', '50_50'));

DROP TRIGGER documents_locked_update;
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
  OR OLD.sha256 IS NOT NEW.sha256 OR OLD.finalized_at IS NOT NEW.finalized_at
  OR OLD.payment_mode IS NOT NEW.payment_mode)
BEGIN SELECT RAISE(ABORT, 'ledger: Dokument ist abgeschlossen und unveränderbar'); END;

CREATE TABLE followups (
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES documents(id),
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL,
  done_at TEXT,
  note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE recurring_invoices (
  id INTEGER PRIMARY KEY,
  customer_id INTEGER NOT NULL REFERENCES customers(id),
  subject TEXT NOT NULL,
  lines_json TEXT NOT NULL,
  interval_months INTEGER NOT NULL DEFAULT 1,
  start_date TEXT NOT NULL,
  count INTEGER,
  issued INTEGER NOT NULL DEFAULT 0,
  next_date TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  source_document_id INTEGER REFERENCES documents(id),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);

ALTER TABLE catalog_items ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0;
UPDATE catalog_items SET sort_order = id;
