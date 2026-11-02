CREATE TABLE app.repair_budget_groups (
 budget_hash text PRIMARY KEY,
 predecessor_hashes jsonb NOT NULL,
 cumulative_limit_micros bigint NOT NULL CHECK(cumulative_limit_micros BETWEEN 1 AND 2000000)
);
CREATE TABLE app.repair_live_profiles (
 profile_hash text PRIMARY KEY REFERENCES app.authorization_budgets,
 budget_hash text NOT NULL REFERENCES app.repair_budget_groups,
 snapshot_hash text NOT NULL REFERENCES app.artifacts,
 authorization_payload jsonb NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now()
);
