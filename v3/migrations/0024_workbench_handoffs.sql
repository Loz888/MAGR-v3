-- V35-U02: content-addressed repair handoffs bound to the workbench ledger.
CREATE TABLE app.workbench_handoffs (
    task_id uuid NOT NULL,
    finding_id char(64) NOT NULL,
    handoff_id char(64) NOT NULL CHECK (handoff_id ~ '^[a-f0-9]{64}$'),
    content_hash char(64) NOT NULL REFERENCES app.artifacts(content_hash),
    payload jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, finding_id),
    FOREIGN KEY (task_id, finding_id)
        REFERENCES app.workbench_findings(task_id, finding_id) ON DELETE CASCADE
);
CREATE INDEX workbench_handoffs_task ON app.workbench_handoffs(task_id, created_at);

CREATE TRIGGER workbench_handoffs_append_only
    BEFORE UPDATE OR DELETE ON app.workbench_handoffs
    FOR EACH ROW EXECUTE FUNCTION app.workbench_append_only();
