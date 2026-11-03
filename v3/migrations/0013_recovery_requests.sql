CREATE TABLE app.recovery_requests (
 request_id uuid PRIMARY KEY,
 run_id uuid NOT NULL REFERENCES app.runs,
 execution_epoch bigint NOT NULL,
 kind text NOT NULL CHECK(kind IN ('no_progress','waiting_input','waiting_approval','outcome_unknown')),
 observation_key text NOT NULL UNIQUE CHECK(length(observation_key)=64),
 evidence jsonb NOT NULL,
 status text NOT NULL DEFAULT 'proposed'
  CHECK(status IN ('proposed','waiting','needs_human','resolved','stale')),
 decision text CHECK(decision IN ('wait','escalate','pause','cancel')),
 decided_by text,
 decided_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX run_events_progress ON app.run_events(run_id,event_id DESC);
CREATE FUNCTION app.protect_recovery_observation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.request_id,NEW.run_id,NEW.execution_epoch,NEW.kind,NEW.observation_key,NEW.evidence,
        NEW.created_at) IS DISTINCT FROM
    ROW(OLD.request_id,OLD.run_id,OLD.execution_epoch,OLD.kind,OLD.observation_key,OLD.evidence,
        OLD.created_at) THEN RAISE EXCEPTION 'Recovery observation is immutable'; END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER recovery_observation_immutable BEFORE UPDATE ON app.recovery_requests
 FOR EACH ROW EXECUTE FUNCTION app.protect_recovery_observation();
