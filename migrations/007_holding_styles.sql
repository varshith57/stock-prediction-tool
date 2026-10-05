-- How each holding is meant to be managed. 'trade': opened from one of the app's buy ideas, so the
-- short-term exit rules apply (stop-loss, trailing stop, target, time stop). 'investment': bought
-- for the long run, never auto-sold by a rule; it gets a review note past a loss the user sets.
-- No row means 'investment'.
CREATE TABLE holding_styles (
    company_id  text PRIMARY KEY,
    style       text NOT NULL CHECK (style IN ('trade', 'investment')),
    updated_at  timestamptz NOT NULL DEFAULT now()
);
