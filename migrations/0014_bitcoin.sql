-- Bitcoin (Spec 18.14, Profilschalter `bitcoin`). Bestand in sats, Beträge in Cent.
-- Bewusst keine Spalte für Adressen, erweiterte öffentliche Schlüssel oder Transaktions-IDs: Importe verwerfen sie,
-- bevor etwas in die Datenbank geht.
CREATE TABLE btc_wallets (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  art TEXT NOT NULL DEFAULT 'wallet' CHECK (art IN ('wallet', 'boerse')),
  aktiv INTEGER NOT NULL DEFAULT 1
);

-- Eine Bewegung je Vorgang. datum = Übergang des wirtschaftlichen Eigentums (Block bzw. Handelszeitpunkt),
-- vertragsdatum nur beim Verkauf (Erlös = vereinbarter Betrag). eur: Kaufbetrag, Vertragsbetrag brutto oder Nennwert
-- der getilgten Forderung bzw. Verbindlichkeit. Eine Zahlung bucht wie ein Beleg über die Bank: Konto und Steuerschlüssel. ak: Anschaffungskosten einer Kundenzahlung, wenn sie vom Nennwert
-- abweichen sollen (Marktwert). Erst bestätigte Bewegungen buchen; verworfene zählen nirgends, ihr
-- import_schluessel verhindert, dass ein erneuter Import sie wieder anlegt.
CREATE TABLE btc_movements (
  id INTEGER PRIMARY KEY,
  wallet_id INTEGER NOT NULL REFERENCES btc_wallets(id),
  ziel_wallet_id INTEGER REFERENCES btc_wallets(id),
  art TEXT NOT NULL CHECK (art IN ('kauf', 'verkauf', 'kundenzahlung', 'zahlung', 'umbuchung', 'eingang', 'ausgang',
                                   'verlust')),
  sats INTEGER NOT NULL CHECK (sats > 0),
  gebuehr_sats INTEGER NOT NULL DEFAULT 0 CHECK (gebuehr_sats >= 0),
  eur INTEGER,
  gebuehr_eur INTEGER NOT NULL DEFAULT 0,
  ak INTEGER,
  vertragsdatum TEXT,
  datum TEXT NOT NULL,
  document_id INTEGER REFERENCES documents(id),
  receipt_id INTEGER REFERENCES receipts(id),
  konto TEXT NOT NULL DEFAULT '',            -- Zahlung einer Eingangsrechnung: Aufwandskonto wie bei der Bank (18.7a)
  steuer TEXT NOT NULL DEFAULT '' CHECK (steuer IN ('', '19', '7', '0', 'rc_eu', 'rc_drittland')),
  payment_id INTEGER REFERENCES payments(id) ON DELETE SET NULL,
  status TEXT NOT NULL DEFAULT 'entwurf' CHECK (status IN ('entwurf', 'bestaetigt', 'verworfen')),
  quelle TEXT NOT NULL DEFAULT 'manuell',
  import_schluessel TEXT UNIQUE,
  notiz TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now','localtime'))
);
CREATE INDEX btc_movements_datum ON btc_movements (datum);

-- Tageskurs (Schlusskurs der Tageskerze, UTC) in EUR je BTC als Dezimaltext; Quelle und Abrufzeit je Kurs.
CREATE TABLE btc_kurse (
  datum TEXT NOT NULL,
  quelle TEXT NOT NULL,
  eur TEXT NOT NULL,
  abgerufen_am TEXT NOT NULL,
  PRIMARY KEY (datum, quelle)
);

-- Saldo, den die Wallet zeigt (von Hand eingetragen); Kontor zeigt die Differenz in sats.
CREATE TABLE btc_wallet_salden (
  wallet_id INTEGER NOT NULL REFERENCES btc_wallets(id),
  datum TEXT NOT NULL,
  sats INTEGER NOT NULL,
  notiz TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (wallet_id, datum)
);

-- Anfangsbestand zum 01.01. eines Jahres (Bestand, Anschaffungskosten, Buchwert zum 31.12. des Vorjahres), wenn die
-- Bewegungen davor nicht im Kontor stehen. Je Wallet die sats; Anschaffungskosten und Buchwert zählen als Summe.
CREATE TABLE btc_vortrag (
  jahr INTEGER NOT NULL,
  wallet_id INTEGER NOT NULL REFERENCES btc_wallets(id),
  sats INTEGER NOT NULL CHECK (sats >= 0),
  ak INTEGER NOT NULL DEFAULT 0,
  buchwert INTEGER NOT NULL DEFAULT 0,
  notiz TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (jahr, wallet_id)
);
