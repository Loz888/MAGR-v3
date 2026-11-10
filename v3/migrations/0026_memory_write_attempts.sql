-- V3.5 M01: durable audit/retry ledger for memory writes.
-- A memory write that fails must not roll back the verified repair or
-- publication state, and the experience must not be silently dropped.
CREATE TABLE IF NOT EXISTS knowledge.memory_write_attempts (
    candidate_id char(64) PRIMARY KEY,
    project_id text NOT NULL,
    repository text NOT NULL,
    actor text NOT NULL,
    subject_key text NOT NULL,
    status text NOT NULL CHECK (
        status IN ('written', 'duplicate', 'rejected', 'failed')
    ),
    error_code text NOT NULL DEFAULT '',
    error_message text NOT NULL DEFAULT '',
    attempts integer NOT NULL DEFAULT 1 CHECK (attempts >= 1),
    candidate jsonb NOT NULL,
    memory_id uuid REFERENCES knowledge.memory_records(memory_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS memory_write_attempts_status_idx
    ON knowledge.memory_write_attempts(status, updated_at DESC);
