-- M9: every Telegram alert sent (summary text only, never amounts), for the weekly cap and the
-- audit. Hard-rule alerts are exempt from the cap (PRD 8.5) but still logged.

CREATE TABLE alerts_sent (
    alert_id   bigserial PRIMARY KEY,
    kind       text NOT NULL CHECK (kind IN ('weekly_plan', 'exit_rule', 'data_failure', 'monthly_audit')),
    dedupe_key text,                          -- e.g. "exit_rule:INFY:2026-10-05" (one per week)
    summary    text NOT NULL,
    sent_at    timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX alerts_sent_dedupe ON alerts_sent (dedupe_key) WHERE dedupe_key IS NOT NULL;
