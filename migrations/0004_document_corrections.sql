-- ledger 0004: Rechnungsberichtigungen (§ 31 Abs. 5 UStDV). Die Originalrechnung bleibt unverändert; jede
-- Berichtigung ist eine neue, schreibgeschützte Fassung mit derselben Nummer und denselben Beträgen.
CREATE TABLE document_corrections (
  id INTEGER PRIMARY KEY,
  document_id INTEGER NOT NULL REFERENCES documents(id),
  version INTEGER NOT NULL,
  reason TEXT NOT NULL,
  snapshot_json TEXT NOT NULL,
  pdf_path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE (document_id, version)
);
CREATE TRIGGER corrections_no_update BEFORE UPDATE ON document_corrections
BEGIN SELECT RAISE(ABORT, 'ledger: Berichtigungen sind unveränderbar'); END;
CREATE TRIGGER corrections_no_delete BEFORE DELETE ON document_corrections
BEGIN SELECT RAISE(ABORT, 'ledger: Berichtigungen sind unveränderbar'); END;
