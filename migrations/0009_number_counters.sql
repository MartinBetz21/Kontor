-- Nummern zählen je Kreis fortlaufend über die Jahre (Spec 6.2, Entscheidung 05.10.2026): Ein Kunde soll an
-- der Nummer nicht ablesen können, der wievielte des Jahres er ist. Das Jahr im Präfix bleibt.
-- `start` ist die letzte Nummer, die vor dem ledger vergeben wurde (z. B. in Lexware); erst danach gilt lückenlos.
CREATE TABLE number_counters (
  prefix TEXT PRIMARY KEY,
  last INTEGER NOT NULL DEFAULT 0,
  start INTEGER NOT NULL DEFAULT 0
);

INSERT INTO number_counters (prefix, last)
  SELECT prefix, last FROM number_sequences s
  WHERE year = (SELECT MAX(year) FROM number_sequences WHERE prefix = s.prefix);

DROP TABLE number_sequences;
