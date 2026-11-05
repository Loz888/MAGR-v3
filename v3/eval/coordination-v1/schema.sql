-- Experiment metadata only. Run explicitly using the Engine's existing locked connection.
CREATE SCHEMA IF NOT EXISTS coordination_eval;
CREATE TABLE IF NOT EXISTS coordination_eval.trials (
    trial_id text PRIMARY KEY,
    manifest_hash text NOT NULL,
    arm text NOT NULL CHECK (arm IN ('D','E','F')),
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TABLE IF NOT EXISTS coordination_eval.events (
    trial_id text NOT NULL REFERENCES coordination_eval.trials,
    event_key text NOT NULL,
    payload jsonb NOT NULL,
    payload_hash text NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    action text,
    event_id uuid,
    run_id uuid,
    completed_at timestamptz,
    reason text,
    PRIMARY KEY (trial_id,event_key)
);
CREATE TABLE IF NOT EXISTS coordination_eval.batches (
    trial_id text NOT NULL REFERENCES coordination_eval.trials,
    ordinal integer NOT NULL,
    event_keys jsonb NOT NULL,
    snapshot jsonb NOT NULL,
    control_run_id uuid,
    plan jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    duration_seconds double precision,
    replay_seconds double precision,
    PRIMARY KEY (trial_id,ordinal)
);
CREATE TABLE IF NOT EXISTS coordination_eval.audit (
    id bigserial PRIMARY KEY,
    trial_id text NOT NULL REFERENCES coordination_eval.trials,
    kind text NOT NULL,
    at timestamptz NOT NULL DEFAULT clock_timestamp(),
    details jsonb NOT NULL
);
CREATE TABLE IF NOT EXISTS coordination_eval.bound_runs (
    run_id uuid PRIMARY KEY REFERENCES app.runs,
    manifest_hash text NOT NULL REFERENCES app.benchmark_grants,
    case_id text NOT NULL,
    arm text NOT NULL CHECK (arm IN ('D','E','F')),
    UNIQUE(manifest_hash,case_id,arm)
);
