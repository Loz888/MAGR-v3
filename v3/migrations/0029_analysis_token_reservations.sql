-- Reserve input+output capacity before each automatic model request.
-- Unknown receipts retain their reservation until explicitly reconciled.
ALTER TABLE app.repo_analysis_runs
    ADD COLUMN reserved_tokens bigint NOT NULL DEFAULT 0 CHECK (reserved_tokens >= 0);
ALTER TABLE app.usage_calls
    ADD COLUMN token_reservation bigint NOT NULL DEFAULT 0 CHECK (token_reservation >= 0);
