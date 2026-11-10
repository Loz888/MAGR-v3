-- Automatic analysis has resource budgets rather than a model/tool call quota.
-- Existing non-null quotas retain the same constraints and meaning.
ALTER TABLE app.authorization_budgets ALTER COLUMN call_limit DROP NOT NULL;
ALTER TABLE app.authorization_budgets
    DROP CONSTRAINT authorization_budgets_used_calls_check;
ALTER TABLE app.authorization_budgets
    ADD CONSTRAINT authorization_budgets_used_calls_check
    CHECK (used_calls >= 0 AND (call_limit IS NULL OR used_calls <= call_limit));
ALTER TABLE app.repo_analysis_runs
    ADD COLUMN output_tokens_used bigint NOT NULL DEFAULT 0 CHECK (output_tokens_used >= 0);
