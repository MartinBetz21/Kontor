-- ledger 0002: Belege mit mehreren Steuersätzen (z. B. Bewirtung: Essen 7 %, Getränke 19 %).
-- Je Beleg eine oder mehrere Teilsummen. receipts.net/tax/gross bleiben die Summen; receipts.tax_kind ist NULL,
-- wenn ein Beleg mehrere Steuerarten hat. Auswertungen lesen nur noch receipt_parts.

CREATE TABLE receipt_parts (
  id INTEGER PRIMARY KEY,
  receipt_id INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
  tax_kind TEXT NOT NULL CHECK (tax_kind IN ('inland_19','inland_7','ohne_vorsteuer','rc_eu','rc_drittland')),
  net INTEGER NOT NULL,
  tax INTEGER NOT NULL
);

INSERT INTO receipt_parts (receipt_id, tax_kind, net, tax)
  SELECT id, tax_kind, net, tax FROM receipts WHERE status = 'geprueft' AND tax_kind IS NOT NULL;

CREATE TRIGGER receipt_parts_lock_insert BEFORE INSERT ON receipt_parts
WHEN EXISTS (SELECT 1 FROM receipts r JOIN period_locks l
  ON l.month IN (substr(r.receipt_date, 1, 7), substr(IFNULL(r.paid_on, ''), 1, 7))
  WHERE r.id = NEW.receipt_id AND r.status = 'geprueft')
BEGIN SELECT RAISE(ABORT, 'ledger: Der Monat ist abgeschlossen'); END;

CREATE TRIGGER receipt_parts_lock_delete BEFORE DELETE ON receipt_parts
WHEN EXISTS (SELECT 1 FROM receipts r JOIN period_locks l
  ON l.month IN (substr(r.receipt_date, 1, 7), substr(IFNULL(r.paid_on, ''), 1, 7))
  WHERE r.id = OLD.receipt_id AND r.status = 'geprueft')
BEGIN SELECT RAISE(ABORT, 'ledger: Der Monat ist abgeschlossen'); END;
