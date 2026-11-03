CREATE TABLE app.worker_heartbeats (
 run_id uuid NOT NULL REFERENCES app.runs,
 execution_epoch bigint NOT NULL,
 last_beat timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL DEFAULT now()+interval '30 seconds',
 sequence bigint NOT NULL DEFAULT 0,
 PRIMARY KEY(run_id,execution_epoch)
);
CREATE TABLE app.dependency_health (
 project_id text NOT NULL,
 repository text NOT NULL,
 dependency text NOT NULL CHECK(dependency IN ('postgres','redis','mcp')),
 state text NOT NULL CHECK(state IN ('ready','unavailable','unconfigured')),
 checked_at timestamptz NOT NULL DEFAULT now(),
 generation bigint NOT NULL DEFAULT 1,
 PRIMARY KEY(project_id,repository,dependency)
);
ALTER TABLE app.recovery_requests ADD COLUMN project_id text;
ALTER TABLE app.recovery_requests ADD COLUMN repository text;
UPDATE app.recovery_requests q SET project_id=h.project_id,repository=h.repository
 FROM app.runs r JOIN app.tickets t USING(ticket_id) JOIN app.threads h USING(thread_id)
 WHERE q.run_id=r.run_id;
ALTER TABLE app.recovery_requests ALTER COLUMN project_id SET NOT NULL;
ALTER TABLE app.recovery_requests ALTER COLUMN repository SET NOT NULL;
ALTER TABLE app.recovery_requests ALTER COLUMN run_id DROP NOT NULL;
ALTER TABLE app.recovery_requests DROP CONSTRAINT recovery_requests_kind_check;
ALTER TABLE app.recovery_requests ADD CHECK(kind IN
 ('no_progress','waiting_input','waiting_approval','outcome_unknown','heartbeat_expired',
  'mailbox_backlog','router_backlog','outbox_backlog','dependency_unavailable'));
CREATE OR REPLACE FUNCTION app.protect_recovery_observation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.request_id,NEW.run_id,NEW.execution_epoch,NEW.kind,NEW.observation_key,NEW.evidence,
        NEW.created_at,NEW.project_id,NEW.repository) IS DISTINCT FROM
    ROW(OLD.request_id,OLD.run_id,OLD.execution_epoch,OLD.kind,OLD.observation_key,OLD.evidence,
        OLD.created_at,OLD.project_id,OLD.repository)
 THEN RAISE EXCEPTION 'Recovery observation is immutable'; END IF;
 RETURN NEW;
END $$;
