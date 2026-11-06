CREATE TABLE app.commerce_templates (
 template_id text NOT NULL,
 project_id text NOT NULL,
 repository text NOT NULL,
 actor text NOT NULL,
 manifest jsonb NOT NULL,
 manifest_hash text NOT NULL CHECK (manifest_hash ~ '^[a-f0-9]{64}$'),
 enabled boolean NOT NULL DEFAULT true,
 PRIMARY KEY(project_id,repository,actor,template_id)
);
CREATE TABLE app.commerce_cases (
 case_id uuid PRIMARY KEY,
 project_id text NOT NULL,
 repository text NOT NULL,
 issue_number bigint NOT NULL CHECK(issue_number>0),
 thread_id uuid NOT NULL REFERENCES app.threads,
 actor text NOT NULL,
 current_revision bigint NOT NULL DEFAULT 1 CHECK(current_revision>0),
 template_id text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(project_id,repository,issue_number),
 FOREIGN KEY(project_id,repository,actor,template_id)
 REFERENCES app.commerce_templates(project_id,repository,actor,template_id)
);
CREATE TABLE app.commerce_case_specs (
 case_id uuid NOT NULL REFERENCES app.commerce_cases,
 revision bigint NOT NULL CHECK(revision>0),
 spec_hash text NOT NULL REFERENCES app.artifacts,
 template_hash text NOT NULL,
 payload jsonb NOT NULL,
 PRIMARY KEY(case_id,revision)
);
CREATE TABLE app.commerce_case_runs (
 case_id uuid NOT NULL REFERENCES app.commerce_cases,
 revision bigint NOT NULL,
 stage text NOT NULL CHECK(stage IN ('investigation','repair_proposal')),
 run_id uuid NOT NULL REFERENCES app.runs,
 input_revision bigint NOT NULL CHECK(input_revision>0),
 preceding_result_id uuid,
 PRIMARY KEY(case_id,revision,stage),
 FOREIGN KEY(case_id,revision) REFERENCES app.commerce_case_specs(case_id,revision)
);
CREATE INDEX commerce_runs_lookup ON app.commerce_case_runs(run_id,revision DESC);
CREATE TABLE app.commerce_requests (
 project_id text NOT NULL,
 actor text NOT NULL,
 request_key text NOT NULL,
 request_hash text NOT NULL,
 case_id uuid NOT NULL REFERENCES app.commerce_cases,
 run_id uuid NOT NULL REFERENCES app.runs,
 PRIMARY KEY(project_id,actor,request_key)
);
CREATE TABLE app.business_results (
 result_id uuid PRIMARY KEY,
 case_id uuid NOT NULL REFERENCES app.commerce_cases,
 case_revision bigint NOT NULL,
 stage text NOT NULL CHECK(stage IN ('investigation','repair_proposal')),
 run_id uuid NOT NULL REFERENCES app.runs,
 input_revision bigint NOT NULL,
 execution_epoch bigint NOT NULL,
 spec_hash text NOT NULL,
 artifact_hash text NOT NULL,
 checker_version text NOT NULL,
 fresh_until timestamptz NOT NULL,
 result_hash text NOT NULL REFERENCES app.artifacts,
 payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(),
 UNIQUE(case_id,case_revision,stage,run_id,input_revision,execution_epoch,spec_hash,
        artifact_hash,checker_version)
);
CREATE TABLE app.commerce_current_results (
 case_id uuid NOT NULL REFERENCES app.commerce_cases,
 case_revision bigint NOT NULL,
 stage text NOT NULL,
 result_id uuid NOT NULL REFERENCES app.business_results,
 PRIMARY KEY(case_id,case_revision,stage)
);
ALTER TABLE app.commerce_case_runs ADD FOREIGN KEY(preceding_result_id)
 REFERENCES app.business_results(result_id);
CREATE FUNCTION app.immutable_business_result() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'business results are append-only'; END $$;
CREATE TRIGGER immutable_business_results BEFORE UPDATE OR DELETE ON app.business_results
 FOR EACH ROW EXECUTE FUNCTION app.immutable_business_result();
CREATE TRIGGER immutable_commerce_specs BEFORE UPDATE OR DELETE ON app.commerce_case_specs
 FOR EACH ROW EXECUTE FUNCTION app.immutable_business_result();

CREATE TABLE app.commerce_action_validations (
 action_id uuid PRIMARY KEY REFERENCES app.actions,
 payload_hash text NOT NULL,
 case_revision bigint NOT NULL,
 input_revision bigint NOT NULL,
 execution_epoch bigint NOT NULL,
 valid_until timestamptz NOT NULL
);

CREATE TABLE app.commerce_live_grants (
 manifest_hash text PRIMARY KEY CHECK(manifest_hash ~ '^[a-f0-9]{64}$'),
 repository text NOT NULL,
 actor text NOT NULL,
 manifest jsonb NOT NULL,
 authorization_payload jsonb NOT NULL,
 max_new_calls integer NOT NULL CHECK(max_new_calls>0 AND max_new_calls<=18),
 max_new_cost_micros bigint NOT NULL CHECK(max_new_cost_micros>0 AND max_new_cost_micros<=55296),
 expires_at timestamptz NOT NULL
);
CREATE TABLE app.commerce_live_runs (
 run_id uuid PRIMARY KEY REFERENCES app.runs,
 manifest_hash text NOT NULL REFERENCES app.commerce_live_grants,
 project_id text NOT NULL,
 issue_number bigint NOT NULL,
 stage text NOT NULL,
 profile_hash text NOT NULL,
 UNIQUE(manifest_hash,project_id,issue_number,stage)
);
