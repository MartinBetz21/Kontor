-- Steuerart im Ausgang (Spec 18.4) und Herkunft (18.3: offene Posten aus Lexware).
-- inland: Sätze je Position wie bisher · drittland_b2b: nicht steuerbar, § 3a Abs. 2 UStG · eu_b2b_rc: Reverse Charge.
ALTER TABLE documents ADD COLUMN tax_treatment TEXT NOT NULL DEFAULT 'inland'
  CHECK (tax_treatment IN ('inland', 'drittland_b2b', 'eu_b2b_rc'));
-- lexware: vor dem ledger ausgestellt; Nummer und PDF stammen aus Lexware, die Rechnung ist schon erklärt.
ALTER TABLE documents ADD COLUMN origin TEXT NOT NULL DEFAULT 'ledger' CHECK (origin IN ('ledger', 'lexware'));
-- Zahlungen, die schon in Lexware gebucht und erklärt sind, zählen in keiner Voranmeldung mehr.
ALTER TABLE payments ADD COLUMN origin TEXT NOT NULL DEFAULT 'ledger' CHECK (origin IN ('ledger', 'lexware'));

CREATE TRIGGER documents_locked_treatment BEFORE UPDATE OF tax_treatment, origin ON documents
WHEN OLD.status <> 'entwurf' AND (OLD.tax_treatment IS NOT NEW.tax_treatment OR OLD.origin IS NOT NEW.origin)
BEGIN SELECT RAISE(ABORT, 'ledger: Dokument ist abgeschlossen und unveränderbar'); END;
