-- V3.4 U01: unified repository workbench ledger.
-- Additive only.  Workbench tasks link the existing analysis and repair runs;
-- they never replace their authority or evidence records.

CREATE TABLE app.workbench_tasks (
    task_id uuid PRIMARY KEY,
    thread_id uuid NOT NULL,
    parent_task_id uuid REFERENCES app.workbench_tasks(task_id),
    request_key text NOT NULL UNIQUE,
    request_digest char(64) NOT NULL,
    mode text NOT NULL CHECK (mode IN ('audit','issue_repair','audit_and_repair')),
    repository_url text NOT NULL,
    repository text NOT NULL,
    resolved_commit char(40) NOT NULL,
    task text NOT NULL,
    issue_number integer,
    execution_mode text NOT NULL CHECK (execution_mode IN ('fixture','live')),
    coverage jsonb NOT NULL,
    observed_coverage jsonb NOT NULL DEFAULT '{}',
    limits jsonb NOT NULL DEFAULT '{}',
    status text NOT NULL CHECK (status IN (
        'queued','auditing','ready','reproducing','repairing','waiting_approval',
        'publishing','completed','failed','paused','cancelled'
    )),
    stage text NOT NULL CHECK (stage IN (
        'suspected','reproduced','patch_verified','waiting_approval','pr_created'
    )),
    audit_run_id uuid REFERENCES app.runs(run_id),
    repair_run_id uuid REFERENCES app.runs(run_id),
    revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX workbench_tasks_created ON app.workbench_tasks(created_at DESC);
CREATE INDEX workbench_tasks_thread ON app.workbench_tasks(thread_id, created_at);

CREATE TABLE app.workbench_events (
    task_id uuid NOT NULL REFERENCES app.workbench_tasks(task_id) ON DELETE CASCADE,
    seq integer NOT NULL CHECK (seq > 0),
    stage text NOT NULL,
    status text NOT NULL,
    summary text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, seq)
);

CREATE TABLE app.workbench_findings (
    task_id uuid NOT NULL REFERENCES app.workbench_tasks(task_id) ON DELETE CASCADE,
    finding_id char(64) NOT NULL,
    stage text NOT NULL CHECK (stage IN (
        'suspected','reproduced','patch_verified','waiting_approval','pr_created'
    )),
    risk_score integer NOT NULL CHECK (risk_score BETWEEN 0 AND 1000),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, finding_id)
);
CREATE INDEX workbench_findings_risk ON app.workbench_findings(task_id, risk_score DESC);

CREATE TABLE app.workbench_memory (
    task_id uuid PRIMARY KEY REFERENCES app.workbench_tasks(task_id) ON DELETE CASCADE,
    payload jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
