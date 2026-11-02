-- Preserve the original one-call limit for existing authorization budgets.
ALTER TABLE app.authorization_budgets
    ADD COLUMN call_limit integer NOT NULL DEFAULT 1 CHECK (call_limit BETWEEN 1 AND 3);
ALTER TABLE app.authorization_budgets
    DROP CONSTRAINT authorization_budgets_used_calls_check;
ALTER TABLE app.authorization_budgets
    ADD CONSTRAINT authorization_budgets_used_calls_check
    CHECK (used_calls >= 0 AND used_calls <= call_limit);
