ALTER TABLE app.actions DROP CONSTRAINT actions_action_type_check;
ALTER TABLE app.actions ADD CONSTRAINT actions_action_type_check CHECK(action_type IN ('issue_comment','draft_pr'));
CREATE TABLE app.repair_requests (
 run_id uuid PRIMARY KEY REFERENCES app.runs,
 snapshot_hash text NOT NULL REFERENCES app.artifacts,
 scope_hash text NOT NULL,
 deadline timestamptz NOT NULL DEFAULT now()+interval '3 minutes',
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.repair_steps (
 run_id uuid NOT NULL REFERENCES app.runs,
 input_revision bigint NOT NULL,
 step_key text NOT NULL,
 execution_epoch bigint NOT NULL,
 status text NOT NULL CHECK(status IN ('running','succeeded')),
 result jsonb,
 PRIMARY KEY(run_id,input_revision,step_key)
);
CREATE TABLE app.repair_candidates (
 run_id uuid NOT NULL REFERENCES app.runs,
 input_revision bigint NOT NULL,
 attempt integer NOT NULL CHECK(attempt BETWEEN 0 AND 2),
 diff_hash text NOT NULL REFERENCES app.artifacts,
 files_hash text NOT NULL REFERENCES app.artifacts,
 workspace_hash text NOT NULL,
 tree_sha text NOT NULL,
 public_receipt jsonb NOT NULL,
 hidden_receipt jsonb NOT NULL,
 verified boolean NOT NULL,
 PRIMARY KEY(run_id,input_revision,attempt)
);
CREATE TABLE app.repair_publication_steps (
 action_id uuid NOT NULL REFERENCES app.actions,
 ordinal integer NOT NULL CHECK(ordinal BETWEEN 0 AND 2),
 kind text NOT NULL CHECK(kind IN ('branch','commit','pull_request')),
 request_hash text NOT NULL,
 status text NOT NULL CHECK(status IN ('dispatching','succeeded','outcome_unknown')),
 receipt jsonb,
 PRIMARY KEY(action_id,ordinal)
);
CREATE TABLE app.action_decisions (
 decision_id uuid PRIMARY KEY,
 action_id uuid NOT NULL REFERENCES app.actions,
 actor text NOT NULL,
 decision text NOT NULL CHECK(decision IN ('reject','edit')),
 payload_hash text NOT NULL,
 reason text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.repair_manual_inputs (
 run_id uuid NOT NULL REFERENCES app.runs,
 input_revision bigint NOT NULL,
 diff_hash text NOT NULL REFERENCES app.artifacts,
 PRIMARY KEY(run_id,input_revision)
);
