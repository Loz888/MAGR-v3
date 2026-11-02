CREATE TABLE app.router_batches (
    batch_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    policy_version text NOT NULL,
    context_hash text NOT NULL CHECK (length(context_hash)=64),
    snapshot jsonb NOT NULL,
    mode text NOT NULL CHECK (mode IN ('fixture','live')),
    status text NOT NULL CHECK (status IN ('prepared','model_running','ready','applied','rejected','failed','usage_unknown')),
    decisions jsonb,
    reason_code text,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE app.runs ADD COLUMN initial_event_ids jsonb NOT NULL DEFAULT '[]';
CREATE TABLE app.router_decisions (
    decision_key text PRIMARY KEY,
    batch_id uuid NOT NULL REFERENCES app.router_batches,
    payload_hash text NOT NULL,
    payload jsonb NOT NULL,
    result jsonb NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.router_audit (
    audit_id bigserial PRIMARY KEY,
    batch_id uuid REFERENCES app.router_batches,
    kind text NOT NULL,
    reason_code text,
    details jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.router_resolutions (
    resolution_id uuid PRIMARY KEY,
    event_id uuid NOT NULL REFERENCES app.trigger_events,
    decision_key text NOT NULL REFERENCES app.router_decisions,
    action text NOT NULL,
    ticket_id uuid REFERENCES app.tickets,
    run_id uuid REFERENCES app.runs,
    superseded_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX current_resolution ON app.router_resolutions(event_id) WHERE superseded_at IS NULL;
CREATE TABLE app.routing_corrections (
    correction_id uuid PRIMARY KEY,
    event_id uuid NOT NULL REFERENCES app.trigger_events,
    previous_decision_key text NOT NULL REFERENCES app.router_decisions,
    actor text NOT NULL,
    reason text NOT NULL,
    thread_revision bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE app.router_inbox ADD COLUMN next_attempt_at timestamptz NOT NULL DEFAULT now();
ALTER TABLE app.router_inbox ADD COLUMN reason_code text;
CREATE TABLE app.ticket_links (
    ticket_id uuid NOT NULL REFERENCES app.tickets,
    related_ticket_id uuid NOT NULL REFERENCES app.tickets,
    decision_key text NOT NULL REFERENCES app.router_decisions DEFERRABLE INITIALLY DEFERRED,
    PRIMARY KEY(ticket_id,related_ticket_id),
    CHECK (ticket_id<>related_ticket_id)
);
CREATE TABLE app.observer_deliveries (
    run_id uuid NOT NULL REFERENCES app.runs,
    event_id uuid NOT NULL REFERENCES app.trigger_events,
    source_thread_id uuid NOT NULL REFERENCES app.threads,
    seq bigint NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(run_id,event_id)
);
CREATE TABLE app.router_calls (
    call_id uuid PRIMARY KEY,
    batch_id uuid NOT NULL UNIQUE REFERENCES app.router_batches,
    budget_profile_hash text NOT NULL REFERENCES app.authorization_budgets,
    router_profile_hash text NOT NULL,
    status text NOT NULL CHECK (status IN ('reserved','known','unknown')),
    reserved_micros bigint NOT NULL CHECK (reserved_micros>0),
    settled_micros bigint CHECK (settled_micros>=0),
    usage jsonb,
    result jsonb,
    response_hash text REFERENCES app.artifacts,
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE app.actions ADD COLUMN reply_kind text NOT NULL DEFAULT 'result' CHECK (reply_kind IN ('result','system'));
ALTER TABLE app.actions ADD COLUMN in_reply_to_event_id uuid REFERENCES app.trigger_events;
CREATE FUNCTION app.protect_routing_metadata() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME='router_batches' THEN
        IF ROW(NEW.batch_id,NEW.project_id,NEW.policy_version,NEW.context_hash,NEW.snapshot,NEW.mode,NEW.expires_at)
          IS DISTINCT FROM ROW(OLD.batch_id,OLD.project_id,OLD.policy_version,OLD.context_hash,OLD.snapshot,OLD.mode,OLD.expires_at) THEN
            RAISE EXCEPTION 'router snapshot is immutable';
        END IF;
    ELSE
        IF ROW(NEW.reply_kind,NEW.in_reply_to_event_id) IS DISTINCT FROM ROW(OLD.reply_kind,OLD.in_reply_to_event_id) THEN
            RAISE EXCEPTION 'reply metadata is immutable';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER immutable_router_snapshot BEFORE UPDATE ON app.router_batches FOR EACH ROW EXECUTE FUNCTION app.protect_routing_metadata();
CREATE TRIGGER immutable_reply_metadata BEFORE UPDATE ON app.actions FOR EACH ROW EXECUTE FUNCTION app.protect_routing_metadata();
