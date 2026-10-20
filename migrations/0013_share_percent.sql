-- Abschlagsrechnung mit allen Positionen: Anteil am Ende statt einer Sammelposition (Spec 6.4, 08.10.2026).
ALTER TABLE documents ADD COLUMN share_percent TEXT;
