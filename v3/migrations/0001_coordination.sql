CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS knowledge;
CREATE SCHEMA IF NOT EXISTS checkpoints;

CREATE TABLE app.threads (
    thread_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    repository text NOT NULL,
    external_key text NOT NULL,
    revision bigint NOT NULL DEFAULT 0 CHECK (revision >= 0),
    latest_seq bigint NOT NULL DEFAULT 0 CHECK (latest_seq >= 0),
    UNIQUE (project_id, external_key)
);
CREATE TABLE app.mandates (
    mandate_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    actor text NOT NULL,
    scope jsonb NOT NULL,
    policy_version text NOT NULL,
    execution_mode text NOT NULL CHECK (execution_mode IN ('fixture','live')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.tickets (
    ticket_id uuid PRIMARY KEY,
    mandate_id uuid NOT NULL REFERENCES app.mandates,
    thread_id uuid NOT NULL REFERENCES app.threads,
    status text NOT NULL CHECK (status IN ('open','ended')),
    outcome text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.runs (
    run_id uuid PRIMARY KEY,
    ticket_id uuid NOT NULL REFERENCES app.tickets,
    attempt_no integer NOT NULL CHECK (attempt_no > 0),
    execution_epoch bigint NOT NULL DEFAULT 0 CHECK (execution_epoch >= 0),
    graph_version text NOT NULL,
    execution_mode text NOT NULL CHECK (execution_mode IN ('fixture','live')),
    status text NOT NULL CHECK (status IN
       ('queued','running','waiting_input','waiting_approval','paused','succeeded','failed','cancelled')),
    outcome text,
    reason_code text,
    question text NOT NULL,
    report_hash text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(ticket_id,attempt_no)
);
CREATE TABLE app.trigger_events (
    event_id uuid PRIMARY KEY,
    source text NOT NULL,
    external_event_id text NOT NULL,
    thread_id uuid NOT NULL REFERENCES app.threads,
    thread_seq bigint NOT NULL CHECK (thread_seq > 0),
    payload_hash text NOT NULL CHECK (length(payload_hash)=64),
    payload jsonb NOT NULL,
    ticket_id uuid REFERENCES app.tickets,
    run_id uuid REFERENCES app.runs,
    route text NOT NULL CHECK (route IN ('created','direct','unrouted')),
    route_snapshot jsonb NOT NULL DEFAULT '[]',
    received_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(source,external_event_id),
    UNIQUE(thread_id,thread_seq)
);
CREATE TABLE app.subscriptions (
    thread_id uuid NOT NULL REFERENCES app.threads,
    run_id uuid NOT NULL REFERENCES app.runs,
    role text NOT NULL CHECK (role IN ('primary','observer','context_only')),
    active boolean NOT NULL DEFAULT true,
    PRIMARY KEY(thread_id,run_id)
);
CREATE UNIQUE INDEX one_active_primary ON app.subscriptions(thread_id)
    WHERE active AND role='primary';
CREATE TABLE app.mailbox (
    run_id uuid NOT NULL REFERENCES app.runs,
    event_id uuid NOT NULL REFERENCES app.trigger_events,
    seq bigint NOT NULL,
    delivered_at timestamptz,
    read_at timestamptz,
    consumed_at timestamptz,
    PRIMARY KEY(run_id,event_id),
    UNIQUE(run_id,seq)
);
CREATE TABLE app.router_inbox (
    event_id uuid PRIMARY KEY REFERENCES app.trigger_events,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.artifacts (
    content_hash text PRIMARY KEY CHECK (content_hash ~ '^[a-f0-9]{64}$'),
    byte_count bigint NOT NULL CHECK (byte_count >= 0),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.evidence (
    evidence_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES app.runs,
    project_id text NOT NULL,
    source_uri text NOT NULL,
    source_version text NOT NULL,
    content_hash text NOT NULL REFERENCES app.artifacts,
    start_line integer NOT NULL CHECK (start_line > 0),
    end_line integer NOT NULL CHECK (end_line >= start_line),
    tool_call_id uuid NOT NULL UNIQUE,
    observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.verdicts (
    verdict_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES app.runs,
    report_hash text NOT NULL REFERENCES app.artifacts,
    passed boolean NOT NULL,
    details jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(run_id,report_hash)
);
CREATE TABLE app.target_write_grants (
    target text PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES app.runs,
    execution_epoch bigint NOT NULL,
    fencing_token bigint NOT NULL CHECK (fencing_token > 0),
    active boolean NOT NULL DEFAULT true
);
CREATE TABLE app.actions (
    action_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES app.runs,
    action_type text NOT NULL CHECK (action_type='issue_comment'),
    target text NOT NULL,
    payload jsonb NOT NULL,
    payload_hash text NOT NULL CHECK (length(payload_hash)=64),
    base_revision bigint NOT NULL,
    execution_epoch bigint NOT NULL,
    fencing_token bigint NOT NULL,
    dedupe_key text NOT NULL UNIQUE,
    status text NOT NULL CHECK (status IN
       ('proposed','approved','ready','dispatching','succeeded','failed','outcome_unknown','expired','cancelled')),
    expires_at timestamptz NOT NULL,
    external_id text,
    lease_token uuid,
    reason_code text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.approvals (
    approval_id uuid PRIMARY KEY,
    action_id uuid NOT NULL UNIQUE REFERENCES app.actions,
    actor text NOT NULL,
    payload_hash text NOT NULL,
    approved_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.outbox (
    outbox_id uuid PRIMARY KEY,
    action_id uuid NOT NULL UNIQUE REFERENCES app.actions,
    status text NOT NULL CHECK (status IN
        ('ready','dispatching','succeeded','failed','outcome_unknown','cancelled')),
    lease_token uuid,
    lease_until timestamptz,
    attempts integer NOT NULL DEFAULT 0,
    next_retry_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.run_events (
    event_id bigserial PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES app.runs,
    kind text NOT NULL,
    safe_payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.budgets (
    mandate_id uuid PRIMARY KEY REFERENCES app.mandates,
    limit_micros bigint NOT NULL CHECK (limit_micros >= 0),
    reserved_micros bigint NOT NULL DEFAULT 0 CHECK (reserved_micros >= 0),
    settled_micros bigint NOT NULL DEFAULT 0 CHECK (settled_micros >= 0),
    CHECK (reserved_micros + settled_micros <= limit_micros)
);
CREATE TABLE app.usage_calls (
    call_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES app.runs,
    step_key text NOT NULL,
    status text NOT NULL CHECK (status IN ('reserved','known','unknown')),
    reserved_micros bigint NOT NULL CHECK (reserved_micros > 0),
    settled_micros bigint CHECK (settled_micros >= 0),
    UNIQUE(run_id,step_key)
);

CREATE FUNCTION app.protect_action_payload() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF ROW(NEW.run_id,NEW.action_type,NEW.target,NEW.payload,NEW.payload_hash,
         NEW.base_revision,NEW.execution_epoch,NEW.fencing_token,NEW.dedupe_key,NEW.expires_at)
      IS DISTINCT FROM
     ROW(OLD.run_id,OLD.action_type,OLD.target,OLD.payload,OLD.payload_hash,
         OLD.base_revision,OLD.execution_epoch,OLD.fencing_token,OLD.dedupe_key,OLD.expires_at) THEN
    RAISE EXCEPTION 'action proposal is immutable';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER immutable_action BEFORE UPDATE ON app.actions
    FOR EACH ROW EXECUTE FUNCTION app.protect_action_payload();
CREATE INDEX mailbox_pending ON app.mailbox(run_id,seq) WHERE consumed_at IS NULL;
CREATE INDEX outbox_ready ON app.outbox(next_retry_at) WHERE status='ready';
