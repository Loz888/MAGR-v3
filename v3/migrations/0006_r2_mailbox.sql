ALTER TABLE app.runs ADD COLUMN mailbox_cursor bigint NOT NULL DEFAULT 0 CHECK (mailbox_cursor >= 0);
ALTER TABLE app.runs ADD COLUMN proposal_generation integer NOT NULL DEFAULT 0 CHECK (proposal_generation >= 0);
ALTER TABLE app.trigger_events ADD COLUMN kind text NOT NULL DEFAULT 'user'
    CHECK (kind IN ('user','tool_result'));
ALTER TABLE app.mailbox ADD COLUMN expires_at timestamptz NOT NULL DEFAULT (now() + interval '7 days');

CREATE TABLE app.run_inputs (
    snapshot_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES app.runs,
    cursor bigint NOT NULL CHECK (cursor > 0),
    input_revision bigint NOT NULL CHECK (input_revision > 0),
    question text NOT NULL CHECK (octet_length(question) <= 65536),
    event_ids jsonb NOT NULL,
    snapshot_hash text NOT NULL CHECK (length(snapshot_hash)=64),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(run_id,cursor)
);
CREATE FUNCTION app.protect_input_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'input snapshots are immutable';
END $$;
CREATE TRIGGER immutable_input_snapshot BEFORE UPDATE OR DELETE ON app.run_inputs
    FOR EACH ROW EXECUTE FUNCTION app.protect_input_snapshot();

CREATE TABLE app.read_jobs (
    job_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES app.runs,
    step_key text NOT NULL,
    execution_epoch bigint NOT NULL,
    status text NOT NULL CHECK (status IN ('queued','running','succeeded','failed','cancelled')),
    lease_token uuid,
    content_hash text REFERENCES app.artifacts,
    source_uri text,
    source_version text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(run_id,step_key)
);
