-- M4: portfolio. Private data: lives only in this database, never in the lake, logs or alerts.
-- Holdings and realised P&L are derived from transactions (FIFO), so editing history is just
-- editing rows here.

CREATE TABLE portfolio_transactions (
    txn_id        bigserial PRIMARY KEY,
    company_id    text NOT NULL,                 -- company master id at entry time
    symbol        text NOT NULL,                 -- symbol as entered / imported
    isin          text,
    side          text NOT NULL CHECK (side IN ('BUY', 'SELL')),
    quantity      integer NOT NULL CHECK (quantity > 0),
    price         numeric(14, 4) NOT NULL CHECK (price > 0),
    trade_date    date,                          -- NULL = unknown (e.g. imported holdings)
    charges       numeric(12, 2) CHECK (charges IS NULL OR charges >= 0),  -- NULL = estimate
    source        text NOT NULL CHECK (source IN ('manual', 'kite_csv')),
    note          text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX portfolio_transactions_company ON portfolio_transactions (company_id, trade_date);
