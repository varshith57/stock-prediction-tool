-- M8: weekly plans and the actions you log against them (feeds the Monthly Audit).

CREATE TABLE weekly_plans (
    plan_id         bigserial PRIMARY KEY,
    signal_date     date NOT NULL UNIQUE,      -- the Friday (last session) the plan was built from
    week_of         date NOT NULL,             -- the Monday it is for
    status          text NOT NULL CHECK (status IN ('OK', 'NO_SIGNAL')),
    action_count    integer NOT NULL,
    payload         jsonb NOT NULL,            -- the full plan (items, reasons, gates, notes)
    model_version   text,
    feature_version text,
    pipeline_version text NOT NULL,
    built_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE plan_actions (
    plan_id   bigint NOT NULL REFERENCES weekly_plans (plan_id) ON DELETE CASCADE,
    item_key  text NOT NULL,                   -- e.g. "BUY:RELIANCE"
    action    text NOT NULL CHECK (action IN ('done', 'partly', 'skipped')),
    reason    text,                            -- optional: price moved, no cash, disagreed...
    acted_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plan_id, item_key)
);
