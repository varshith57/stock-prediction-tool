-- M10: locked audit snapshots (a review is frozen as it was, then exported or revisited).

CREATE TABLE audit_snapshots (
    snapshot_id   bigserial PRIMARY KEY,
    period_start  date NOT NULL,
    period_end    date NOT NULL,
    payload       jsonb NOT NULL,
    locked_at     timestamptz NOT NULL DEFAULT now(),
    pipeline_version text NOT NULL
);
