-- Settings edited in the app. Each save is a new version (full values + what changed), so any
-- version can be restored and every plan records the settings it was built with.

CREATE TABLE settings_versions (
    version_id  bigserial PRIMARY KEY,
    values      jsonb NOT NULL,
    changed     jsonb NOT NULL DEFAULT '{}',   -- {"risk.max_stock_weight": [0.15, 0.12], ...}
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now()
);

-- When settings change, the current week's plan is rebuilt in place (its logged actions stay);
-- the replaced payload is kept here so the audit can still see what was shown before.
CREATE TABLE weekly_plan_revisions (
    plan_id      bigint NOT NULL REFERENCES weekly_plans (plan_id) ON DELETE CASCADE,
    payload      jsonb NOT NULL,
    replaced_at  timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE weekly_plans ADD COLUMN settings_version bigint;
