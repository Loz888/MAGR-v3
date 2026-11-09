-- V3.2: read-only repository analysis runs, tool ledger and evidence-bound claims.
-- Additive only.  Existing authorization budgets keep their original limits.

ALTER TABLE app.authorization_budgets
    DROP CONSTRAINT authorization_budgets_call_limit_check;
ALTER TABLE app.authorization_budgets
    ADD CONSTRAINT authorization_budgets_call_limit_check
    CHECK (call_limit BETWEEN 1 AND 100);

CREATE TABLE app.repo_analysis_runs (
    run_id uuid PRIMARY KEY REFERENCES app.runs ON DELETE CASCADE,
    request_key text NOT NULL UNIQUE,
    request_digest char(64) NOT NULL,
    repository text NOT NULL,
    requested_ref text,
    resolved_commit char(40) NOT NULL,
    task text NOT NULL,
    execution_mode text NOT NULL CHECK (execution_mode IN ('fixture','live')),
    limits jsonb NOT NULL,
    model jsonb NOT NULL,
    deadline_at timestamptz NOT NULL,
    model_calls_used integer NOT NULL DEFAULT 0 CHECK (model_calls_used >= 0),
    tool_calls_used integer NOT NULL DEFAULT 0 CHECK (tool_calls_used >= 0),
    input_tokens_used bigint NOT NULL DEFAULT 0 CHECK (input_tokens_used >= 0),
    stop_reason text,
    report_hash text REFERENCES app.artifacts,
    authorization_hash char(64) NOT NULL,
    profile_hash char(64) NOT NULL,
    parent_run_id uuid REFERENCES app.runs,
    prior_report_hash text REFERENCES app.artifacts,
    prior_evidence_map jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX repo_analysis_runs_created ON app.repo_analysis_runs(created_at DESC);
CREATE INDEX repo_analysis_runs_parent ON app.repo_analysis_runs(parent_run_id);

CREATE TABLE app.repo_tool_calls (
    call_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES app.runs,
    step_key text NOT NULL,
    tool text NOT NULL,
    args_hash char(64) NOT NULL,
    status text NOT NULL CHECK (status IN ('running','succeeded','failed','unknown')),
    result_hash text REFERENCES app.artifacts,
    result_uri text,
    result_version text,
    byte_count bigint CHECK (byte_count >= 0),
    evidence_kind text,
    evidence_id uuid REFERENCES app.evidence,
    error_code text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    UNIQUE(run_id, step_key)
);
CREATE INDEX repo_tool_calls_run ON app.repo_tool_calls(run_id, created_at);

CREATE TABLE app.repo_analysis_claims (
    claim_id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES app.runs,
    section text NOT NULL CHECK (section IN ('findings','key_files','call_chains')),
    statement text NOT NULL,
    evidence_id uuid NOT NULL REFERENCES app.evidence,
    quote text NOT NULL,
    start_line integer NOT NULL CHECK (start_line > 0),
    end_line integer NOT NULL CHECK (end_line >= start_line),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX repo_analysis_claims_run ON app.repo_analysis_claims(run_id, section);
