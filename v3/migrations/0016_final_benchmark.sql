CREATE TABLE app.repository_cost_caps (
 repository text PRIMARY KEY,
 limit_micros bigint NOT NULL DEFAULT 2000000 CHECK(limit_micros=2000000)
);
CREATE TABLE app.benchmark_grants (
 manifest_hash text PRIMARY KEY,
 manifest jsonb NOT NULL,
 authorization_payload jsonb NOT NULL,
 budget_hash text NOT NULL UNIQUE REFERENCES app.authorization_budgets,
 created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.benchmark_runs (
 run_id uuid PRIMARY KEY REFERENCES app.runs,
 manifest_hash text NOT NULL REFERENCES app.benchmark_grants,
 case_id text NOT NULL,
 arm text NOT NULL CHECK(arm IN ('A','B','C')),
 UNIQUE(manifest_hash,case_id,arm)
);
CREATE FUNCTION app.protect_benchmark_grant() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Benchmark grants are immutable'; END $$;
CREATE TRIGGER benchmark_grant_immutable BEFORE UPDATE OR DELETE ON app.benchmark_grants
 FOR EACH ROW EXECUTE FUNCTION app.protect_benchmark_grant();
