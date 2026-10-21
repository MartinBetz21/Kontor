-- ledger 0003: Herkunft eines Belegs (z. B. telegram:<message_id>, damit Nachträge den Beleg finden)
-- und die Pflichtangaben eines Bewirtungsbelegs (§ 4 Abs. 5 Nr. 2 EStG): Anlass und bewirtete Personen.
ALTER TABLE receipts ADD COLUMN source_ref TEXT;
ALTER TABLE receipts ADD COLUMN bewirtung_anlass TEXT NOT NULL DEFAULT '';
ALTER TABLE receipts ADD COLUMN bewirtung_teilnehmer TEXT NOT NULL DEFAULT '';
CREATE INDEX receipts_source_ref ON receipts (source_ref);
