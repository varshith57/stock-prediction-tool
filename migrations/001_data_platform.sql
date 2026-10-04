-- M1: data platform tables (app database). Market data itself lives in the Parquet lake.

-- Every source we fetch from, its terms and its health. Seeded from src/stockapp/ingest/sources.yaml.
CREATE TABLE source_registry (
    source_id             text PRIMARY KEY,
    name                  text NOT NULL,
    provides              text NOT NULL,
    access                text NOT NULL,
    url_template          text,
    terms_note            text NOT NULL,
    fallback              text,
    earliest_date         date,            -- NULL until probed; never assumed
    earliest_date_note    text,
    health                text NOT NULL DEFAULT 'unknown'
                          CHECK (health IN ('unknown', 'ok', 'degraded', 'blocked', 'format_changed')),
    consecutive_failures  integer NOT NULL DEFAULT 0,
    last_success_at       timestamptz,
    last_failure_at       timestamptz,
    last_failure_reason   text,
    updated_at            timestamptz NOT NULL DEFAULT now()
);

-- One row per job execution (one connector run for one partition, or a pipeline step).
CREATE TABLE job_runs (
    job_run_id        bigserial PRIMARY KEY,
    job               text NOT NULL,
    source_id         text REFERENCES source_registry (source_id),
    partition_key     text,
    status            text NOT NULL CHECK (status IN ('running', 'success', 'skipped', 'not_available', 'failed')),
    started_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    rows_loaded       integer,
    message           text,
    pipeline_version  text NOT NULL
);
CREATE INDEX job_runs_source_partition ON job_runs (source_id, partition_key, started_at DESC);

-- Manifest of every raw file archived in the lake (lineage: any silver row traces back here).
CREATE TABLE source_files (
    source_file_id      bigserial PRIMARY KEY,
    source_id           text NOT NULL REFERENCES source_registry (source_id),
    partition_key       text NOT NULL,
    url                 text NOT NULL,
    lake_path           text NOT NULL,
    sha256              char(64) NOT NULL,
    size_bytes          bigint NOT NULL,
    fetched_at          timestamptz NOT NULL,
    schema_fingerprint  text,
    status              text NOT NULL CHECK (status IN ('archived', 'loaded', 'quarantined')),
    job_run_id          bigint REFERENCES job_runs (job_run_id),
    UNIQUE (source_id, partition_key, sha256)
);

-- Last good partition per source, so incremental runs know where to resume.
CREATE TABLE watermarks (
    source_id       text PRIMARY KEY REFERENCES source_registry (source_id),
    last_good_key   text NOT NULL,
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Anything that failed a check. Never auto-fixed; resolved by a person or a code fix.
CREATE TABLE quarantine (
    quarantine_id    bigserial PRIMARY KEY,
    source_id        text NOT NULL REFERENCES source_registry (source_id),
    partition_key    text NOT NULL,
    source_file_id   bigint REFERENCES source_files (source_file_id),
    severity         text NOT NULL CHECK (severity IN ('BLOCK', 'WARN')),
    reason           text NOT NULL,
    detail           jsonb,
    created_at       timestamptz NOT NULL DEFAULT now(),
    resolved_at      timestamptz,
    resolution       text
);
CREATE INDEX quarantine_open ON quarantine (source_id, partition_key) WHERE resolved_at IS NULL;

-- Exchange calendar. Holidays come from the exchange's published list; sessions are dates for which
-- the exchange actually published a daily file (authoritative for the past, incl. special sessions).
CREATE TABLE trading_holidays (
    exchange      text NOT NULL,
    segment       text NOT NULL,
    holiday_date  date NOT NULL,
    description   text NOT NULL,
    source        text NOT NULL,           -- 'nse_holiday_api' or 'inferred_no_file'
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (exchange, segment, holiday_date)
);

CREATE TABLE trading_sessions (
    exchange      text NOT NULL,
    segment       text NOT NULL,
    session_date  date NOT NULL,
    evidence      text NOT NULL,           -- e.g. 'nse_udiff_bhavcopy'
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (exchange, segment, session_date)
);
