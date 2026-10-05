-- Fundamentals backfill bookkeeping: one row per XBRL file tried, so the overnight download can
-- stop at any point and resume where it left off.
CREATE TABLE xbrl_fetches (
    url         text PRIMARY KEY,
    company_id  text NOT NULL,
    period_to   date NOT NULL,
    status      text NOT NULL CHECK (status IN ('parsed', 'no_quarter', 'missing', 'error')),
    detail      text,
    fetched_at  timestamptz NOT NULL DEFAULT now()
);
