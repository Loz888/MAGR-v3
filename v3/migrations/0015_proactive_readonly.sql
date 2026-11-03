CREATE TABLE app.proactive_policies (
 policy_hash text PRIMARY KEY,
 project_id text NOT NULL,
 repository text NOT NULL,
 definition jsonb NOT NULL,
 mode text NOT NULL DEFAULT 'observe' CHECK(mode IN ('observe','auto','revoked')),
 activated_by text,
 activated_at timestamptz,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.speculation_budgets (
 policy_hash text PRIMARY KEY REFERENCES app.proactive_policies,
 run_limit integer NOT NULL CHECK(run_limit BETWEEN 1 AND 10),
 read_limit integer NOT NULL CHECK(read_limit BETWEEN 1 AND 200),
 call_limit integer NOT NULL CHECK(call_limit BETWEEN 0 AND 3),
 cost_limit_micros bigint NOT NULL CHECK(cost_limit_micros>=0),
 used_runs integer NOT NULL DEFAULT 0,
 used_reads integer NOT NULL DEFAULT 0,
 used_calls integer NOT NULL DEFAULT 0,
 reserved_micros bigint NOT NULL DEFAULT 0,
 settled_micros bigint NOT NULL DEFAULT 0,
 CHECK(used_runs BETWEEN 0 AND run_limit AND used_reads BETWEEN 0 AND read_limit
       AND used_calls BETWEEN 0 AND call_limit),
 CHECK(reserved_micros>=0 AND settled_micros>=0
       AND reserved_micros+settled_micros<=cost_limit_micros)
);
CREATE TABLE app.proactive_observations (
 observation_id uuid PRIMARY KEY,
 policy_hash text NOT NULL REFERENCES app.proactive_policies,
 external_event_id text NOT NULL,
 payload_hash text NOT NULL,
 payload jsonb NOT NULL,
 decision text NOT NULL,
 run_id uuid REFERENCES app.runs,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(policy_hash,external_event_id)
);
CREATE TABLE app.speculative_runs (
 run_id uuid PRIMARY KEY REFERENCES app.runs,
 policy_hash text NOT NULL REFERENCES app.proactive_policies
);
CREATE TABLE app.proactive_event_claims (
 project_id text NOT NULL,
 repository text NOT NULL,
 external_event_id text NOT NULL,
 payload_hash text NOT NULL,
 run_id uuid NOT NULL REFERENCES app.runs,
 PRIMARY KEY(project_id,repository,external_event_id)
);
CREATE FUNCTION app.protect_proactive_limits() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF TG_TABLE_NAME='proactive_policies' THEN
  IF ROW(NEW.policy_hash,NEW.project_id,NEW.repository,NEW.definition) IS DISTINCT FROM
     ROW(OLD.policy_hash,OLD.project_id,OLD.repository,OLD.definition)
  THEN RAISE EXCEPTION 'Policy definition is immutable'; END IF;
 ELSE
  IF ROW(NEW.policy_hash,NEW.run_limit,NEW.read_limit,NEW.call_limit,NEW.cost_limit_micros)
     IS DISTINCT FROM
     ROW(OLD.policy_hash,OLD.run_limit,OLD.read_limit,OLD.call_limit,OLD.cost_limit_micros)
  THEN RAISE EXCEPTION 'Speculation limits are immutable'; END IF;
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER policy_immutable BEFORE UPDATE ON app.proactive_policies
 FOR EACH ROW EXECUTE FUNCTION app.protect_proactive_limits();
CREATE TRIGGER speculation_limits_immutable BEFORE UPDATE ON app.speculation_budgets
 FOR EACH ROW EXECUTE FUNCTION app.protect_proactive_limits();
