-- V3.5 U01: unified task/run ledger with server-side phase transitions.
-- Additive only. Historic rows are backfilled forward; no existing column or
-- table is removed, and repo-analysis/R5 APIs keep their legacy status field.

ALTER TABLE app.workbench_tasks
    ADD COLUMN phase text NOT NULL DEFAULT 'queued',
    ADD COLUMN execution_epoch integer NOT NULL DEFAULT 1 CHECK (execution_epoch > 0),
    ADD COLUMN resume_phase text NOT NULL DEFAULT 'queued',
    ADD COLUMN verifier_run_id uuid REFERENCES app.runs(run_id);

UPDATE app.workbench_tasks
SET phase = CASE status
    WHEN 'queued' THEN 'queued'
    WHEN 'auditing' THEN 'investigating'
    WHEN 'ready' THEN 'suspected'
    WHEN 'reproducing' THEN 'reproduced'
    WHEN 'repairing' THEN 'repairing'
    WHEN 'waiting_approval' THEN 'waiting_approval'
    WHEN 'publishing' THEN 'publishing'
    WHEN 'completed' THEN 'completed'
    WHEN 'failed' THEN 'failed'
    WHEN 'paused' THEN 'queued'
    WHEN 'cancelled' THEN 'cancelled'
    ELSE 'queued'
END,
resume_phase = CASE status
    WHEN 'paused' THEN 'queued'
    ELSE phase
END;

UPDATE app.workbench_tasks SET resume_phase = phase WHERE status <> 'paused';

ALTER TABLE app.workbench_tasks
    ADD CONSTRAINT workbench_tasks_phase_check CHECK (phase IN (
        'queued','investigating','suspected','reproduced','repairing',
        'patch_verified','waiting_approval','publishing','pr_created',
        'completed','waiting_input','paused','failed','cancelled'
    )),
    ADD CONSTRAINT workbench_tasks_resume_phase_check CHECK (resume_phase IN (
        'queued','investigating','suspected','reproduced','repairing',
        'patch_verified','waiting_approval','publishing','pr_created',
        'completed','waiting_input','paused','failed','cancelled'
    ));

CREATE TABLE app.workbench_phase_transitions (
    from_phase text NOT NULL,
    to_phase text NOT NULL,
    PRIMARY KEY (from_phase, to_phase)
);

INSERT INTO app.workbench_phase_transitions(from_phase, to_phase) VALUES
    ('queued','investigating'),
    ('queued','suspected'),
    ('queued','cancelled'),
    ('queued','failed'),
    ('queued','paused'),
    ('investigating','suspected'),
    ('investigating','failed'),
    ('investigating','cancelled'),
    ('investigating','paused'),
    ('investigating','waiting_input'),
    ('suspected','reproduced'),
    ('suspected','completed'),
    ('suspected','failed'),
    ('suspected','cancelled'),
    ('suspected','paused'),
    ('suspected','waiting_input'),
    ('reproduced','repairing'),
    ('reproduced','suspected'),
    ('reproduced','failed'),
    ('reproduced','cancelled'),
    ('reproduced','paused'),
    ('repairing','patch_verified'),
    ('repairing','reproduced'),
    ('repairing','failed'),
    ('repairing','cancelled'),
    ('repairing','paused'),
    ('patch_verified','waiting_approval'),
    ('patch_verified','repairing'),
    ('patch_verified','failed'),
    ('patch_verified','cancelled'),
    ('patch_verified','paused'),
    ('waiting_approval','publishing'),
    ('waiting_approval','failed'),
    ('waiting_approval','cancelled'),
    ('waiting_approval','paused'),
    ('publishing','pr_created'),
    ('publishing','waiting_approval'),
    ('publishing','failed'),
    ('publishing','cancelled'),
    ('publishing','paused'),
    ('waiting_input','investigating'),
    ('waiting_input','suspected'),
    ('waiting_input','repairing'),
    ('waiting_input','cancelled'),
    ('waiting_input','failed'),
    ('waiting_input','paused'),
    ('paused','queued'),
    ('paused','investigating'),
    ('paused','suspected'),
    ('paused','reproduced'),
    ('paused','repairing'),
    ('paused','patch_verified'),
    ('paused','waiting_approval'),
    ('paused','publishing'),
    ('paused','cancelled'),
    ('paused','failed'),
    ('failed','queued'),
    ('failed','cancelled');

CREATE TABLE app.workbench_transitions (
    task_id uuid NOT NULL REFERENCES app.workbench_tasks(task_id) ON DELETE CASCADE,
    revision integer NOT NULL CHECK (revision > 0),
    from_phase text NOT NULL,
    to_phase text NOT NULL,
    from_status text NOT NULL,
    to_status text NOT NULL,
    execution_epoch integer NOT NULL,
    actor text NOT NULL,
    reason text NOT NULL DEFAULT '',
    event_seq integer,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, revision)
);

CREATE TABLE app.workbench_run_links (
    task_id uuid NOT NULL REFERENCES app.workbench_tasks(task_id) ON DELETE CASCADE,
    run_kind text NOT NULL CHECK (run_kind IN ('audit','repair','verifier')),
    run_id uuid NOT NULL REFERENCES app.runs(run_id),
    ordinal integer NOT NULL CHECK (ordinal > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, run_kind, run_id),
    UNIQUE (task_id, run_kind, ordinal)
);

ALTER TABLE app.workbench_events
    ADD COLUMN kind text NOT NULL DEFAULT 'task',
    ADD COLUMN idempotency_key text,
    ADD COLUMN execution_epoch integer NOT NULL DEFAULT 1;
CREATE UNIQUE INDEX workbench_events_idempotency
    ON app.workbench_events(task_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE OR REPLACE FUNCTION app.workbench_task_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.task_id IS DISTINCT FROM OLD.task_id
       OR NEW.request_key IS DISTINCT FROM OLD.request_key
       OR NEW.request_digest IS DISTINCT FROM OLD.request_digest
       OR NEW.mode IS DISTINCT FROM OLD.mode
       OR NEW.repository_url IS DISTINCT FROM OLD.repository_url
       OR NEW.repository IS DISTINCT FROM OLD.repository
       OR NEW.resolved_commit IS DISTINCT FROM OLD.resolved_commit
       OR NEW.execution_mode IS DISTINCT FROM OLD.execution_mode
       OR NEW.coverage IS DISTINCT FROM OLD.coverage
       OR NEW.limits IS DISTINCT FROM OLD.limits THEN
        RAISE EXCEPTION 'WORKBENCH_IDENTITY_IMMUTABLE';
    END IF;
    IF NEW.revision <> OLD.revision + 1 THEN
        RAISE EXCEPTION 'WORKBENCH_REVISION_CAS';
    END IF;
    IF NEW.execution_epoch < OLD.execution_epoch THEN
        RAISE EXCEPTION 'WORKBENCH_STALE_EPOCH';
    END IF;
    IF NEW.phase IS DISTINCT FROM OLD.phase THEN
        IF NOT EXISTS (
            SELECT 1 FROM app.workbench_phase_transitions
            WHERE from_phase = OLD.phase AND to_phase = NEW.phase
        ) THEN
            RAISE EXCEPTION 'WORKBENCH_PHASE_TRANSITION_DENIED';
        END IF;
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER workbench_tasks_guard
    BEFORE UPDATE ON app.workbench_tasks
    FOR EACH ROW EXECUTE FUNCTION app.workbench_task_guard();

CREATE OR REPLACE FUNCTION app.workbench_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'WORKBENCH_APPEND_ONLY';
END $$;

CREATE TRIGGER workbench_events_append_only
    BEFORE UPDATE OR DELETE ON app.workbench_events
    FOR EACH ROW EXECUTE FUNCTION app.workbench_append_only();

CREATE TRIGGER workbench_transitions_append_only
    BEFORE UPDATE OR DELETE ON app.workbench_transitions
    FOR EACH ROW EXECUTE FUNCTION app.workbench_append_only();
