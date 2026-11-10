-- V3.5 R04: Workbench Draft PR publication through a durable Engine outbox.
-- Additive only.  A workbench approval enqueues one publication action; the
-- HTTP request never performs a GitHub write.  A leased dispatcher writes
-- branch, commit and Draft PR one receipted step at a time.

CREATE TABLE app.workbench_publications (
    publication_id uuid PRIMARY KEY,
    task_id uuid NOT NULL REFERENCES app.workbench_tasks(task_id) ON DELETE CASCADE,
    finding_id char(64) NOT NULL,
    execution_mode text NOT NULL CHECK (execution_mode IN ('fixture','live')),
    repository text NOT NULL,
    base_branch text NOT NULL,
    base_sha char(40) NOT NULL,
    tree_hash char(64) NOT NULL,
    proposal_id char(64) NOT NULL,
    branch text NOT NULL,
    payload_hash char(64) NOT NULL,
    diff_sha256 char(64) NOT NULL,
    approval_id char(64) NOT NULL,
    actor text NOT NULL,
    approved_at text NOT NULL,
    bundle jsonb NOT NULL,
    status text NOT NULL CHECK (status IN (
        'ready','dispatching','succeeded','failed','outcome_unknown',
        'exported','stale','cancelled'
    )),
    reason_code text NOT NULL DEFAULT '',
    reason text NOT NULL DEFAULT '',
    external_id text,
    pull_request jsonb,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_retry_at timestamptz NOT NULL DEFAULT now(),
    lease_token uuid,
    lease_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (task_id, finding_id, payload_hash)
);
CREATE INDEX workbench_publications_ready
    ON app.workbench_publications(execution_mode, status, next_retry_at);
CREATE INDEX workbench_publications_task
    ON app.workbench_publications(task_id, finding_id, created_at DESC);

CREATE TABLE app.workbench_publication_steps (
    publication_id uuid NOT NULL
        REFERENCES app.workbench_publications(publication_id) ON DELETE CASCADE,
    ordinal integer NOT NULL CHECK (ordinal BETWEEN 0 AND 2),
    kind text NOT NULL CHECK (kind IN ('branch','commit','pull_request')),
    status text NOT NULL CHECK (status IN ('dispatching','succeeded','outcome_unknown')),
    request_hash char(64) NOT NULL,
    receipt jsonb,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (publication_id, ordinal)
);
