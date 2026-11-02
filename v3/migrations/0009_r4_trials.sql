-- New explicitly granted trial budgets never reset an earlier exhausted ledger.
ALTER TABLE app.authorization_budgets DROP CONSTRAINT authorization_budgets_call_limit_check;
ALTER TABLE app.authorization_budgets ADD CONSTRAINT authorization_budgets_call_limit_check CHECK(call_limit BETWEEN 1 AND 100);
CREATE TABLE app.r4_trials (
    trial_id uuid PRIMARY KEY,
    manifest_hash text NOT NULL UNIQUE,
    manifest jsonb NOT NULL,
    predecessor_budget_hash text NOT NULL UNIQUE REFERENCES app.authorization_budgets,
    budget_hash text NOT NULL UNIQUE REFERENCES app.authorization_budgets,
    authorization_payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.r4_trial_profiles (
    profile_hash text PRIMARY KEY REFERENCES app.authorization_budgets,
    trial_id uuid NOT NULL REFERENCES app.r4_trials,
    case_id text NOT NULL,
    arm text NOT NULL CHECK(arm IN ('multi','single')),
    UNIQUE(trial_id,case_id,arm)
);
ALTER TABLE app.usage_calls ADD COLUMN trial_budget_hash text REFERENCES app.authorization_budgets;
CREATE TABLE app.r4_analyses (
    run_id uuid NOT NULL REFERENCES app.runs,
    input_revision bigint NOT NULL,
    content_hash text NOT NULL REFERENCES app.artifacts,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(run_id,input_revision)
);
