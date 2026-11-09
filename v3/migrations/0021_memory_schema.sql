-- V3.3 M01: unified memory identity model over the legacy knowledge.memories table.
-- Additive only.  Legacy rows stay in place and remain readable without a rewrite.

CREATE TABLE knowledge.memory_records (
    memory_id uuid PRIMARY KEY,
    memory_kind text NOT NULL CHECK (memory_kind IN ('working','episodic','semantic','procedural')),
    project_id text NOT NULL,
    repository text NOT NULL CHECK (repository = lower(repository)),
    commit_sha char(40),
    actor text NOT NULL,
    scope_kind text NOT NULL CHECK (scope_kind IN ('project','repository','commit')),
    scope_key text NOT NULL,
    subject_key text NOT NULL CHECK (length(subject_key) BETWEEN 1 AND 200),
    summary_hash text NOT NULL REFERENCES app.artifacts(content_hash),
    evidence_ids jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(evidence_ids) = 'array'),
    artifact_hashes jsonb NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(artifact_hashes) = 'array'),
    confidence numeric(4,3) NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    verification_status text NOT NULL
        CHECK (verification_status IN ('unverified','supported','verified','disputed')),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','superseded','revoked','stale')),
    -- Deferred so a supersede can point at the replacement inserted later in the same TX.
    superseded_by uuid REFERENCES knowledge.memory_records(memory_id)
        DEFERRABLE INITIALLY DEFERRED,
    revoked_at timestamptz,
    revoked_reason text CHECK (revoked_reason IS NULL OR length(revoked_reason) BETWEEN 1 AND 500),
    stale_at timestamptz,
    stale_reason text CHECK (stale_reason IS NULL OR length(stale_reason) BETWEEN 1 AND 500),
    legacy_memory_id uuid UNIQUE REFERENCES knowledge.memories(memory_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK ((scope_kind = 'commit') = (commit_sha IS NOT NULL)),
    CHECK (
        scope_key = CASE scope_kind
            WHEN 'project' THEN project_id
            WHEN 'repository' THEN repository
            ELSE commit_sha::text
        END
    ),
    CHECK ((status = 'superseded') = (superseded_by IS NOT NULL)),
    CHECK ((status = 'revoked') = (revoked_at IS NOT NULL)),
    CHECK (status <> 'stale' OR stale_at IS NOT NULL),
    CHECK (superseded_by IS NULL OR superseded_by <> memory_id)
);
CREATE UNIQUE INDEX memory_one_current_identity ON knowledge.memory_records
    (project_id,repository,actor,memory_kind,scope_kind,scope_key,subject_key)
    WHERE status IN ('active','stale');
CREATE INDEX memory_recall_identity ON knowledge.memory_records
    (project_id,repository,actor,status,created_at DESC);
CREATE INDEX memory_commit_scope ON knowledge.memory_records
    (project_id,repository,commit_sha) WHERE commit_sha IS NOT NULL;
CREATE INDEX memory_evidence_refs ON knowledge.memory_records USING gin(evidence_ids);

-- Four kinds share one identity table; kind-specific payloads stay in narrow tables.
CREATE TABLE knowledge.memory_working (
    memory_id uuid PRIMARY KEY REFERENCES knowledge.memory_records(memory_id),
    run_id uuid NOT NULL REFERENCES app.runs,
    expires_at timestamptz NOT NULL
);
CREATE INDEX memory_working_expiry ON knowledge.memory_working(expires_at);

CREATE TABLE knowledge.memory_episodic (
    memory_id uuid PRIMARY KEY REFERENCES knowledge.memory_records(memory_id),
    ticket_id uuid NOT NULL REFERENCES app.tickets,
    run_id uuid NOT NULL REFERENCES app.runs,
    event_kind text NOT NULL CHECK (length(event_kind) BETWEEN 1 AND 100),
    occurred_at timestamptz NOT NULL
);
CREATE INDEX memory_episodic_ticket ON knowledge.memory_episodic(ticket_id);

CREATE TABLE knowledge.memory_semantic (
    memory_id uuid PRIMARY KEY REFERENCES knowledge.memory_records(memory_id),
    subject text NOT NULL CHECK (length(subject) BETWEEN 1 AND 200),
    predicate text NOT NULL CHECK (length(predicate) BETWEEN 1 AND 200),
    object_value text NOT NULL CHECK (length(object_value) BETWEEN 1 AND 2000)
);
CREATE INDEX memory_semantic_subject ON knowledge.memory_semantic(subject,predicate);

CREATE TABLE knowledge.memory_procedural (
    memory_id uuid PRIMARY KEY REFERENCES knowledge.memory_records(memory_id),
    task_key text NOT NULL CHECK (length(task_key) BETWEEN 1 AND 200),
    recipe_hash text NOT NULL REFERENCES app.artifacts(content_hash),
    success_count integer NOT NULL DEFAULT 0 CHECK (success_count >= 0),
    failure_count integer NOT NULL DEFAULT 0 CHECK (failure_count >= 0)
);
CREATE INDEX memory_procedural_task ON knowledge.memory_procedural(task_key);

-- Content is append-only: only lifecycle fields may change, and terminal states stay terminal.
CREATE FUNCTION knowledge.protect_memory_record() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'memory records are append-only; revoke instead';
    END IF;
    IF NEW.memory_id IS DISTINCT FROM OLD.memory_id
       OR NEW.memory_kind IS DISTINCT FROM OLD.memory_kind
       OR NEW.project_id IS DISTINCT FROM OLD.project_id
       OR NEW.repository IS DISTINCT FROM OLD.repository
       OR NEW.commit_sha IS DISTINCT FROM OLD.commit_sha
       OR NEW.actor IS DISTINCT FROM OLD.actor
       OR NEW.scope_kind IS DISTINCT FROM OLD.scope_kind
       OR NEW.scope_key IS DISTINCT FROM OLD.scope_key
       OR NEW.subject_key IS DISTINCT FROM OLD.subject_key
       OR NEW.summary_hash IS DISTINCT FROM OLD.summary_hash
       OR NEW.evidence_ids IS DISTINCT FROM OLD.evidence_ids
       OR NEW.artifact_hashes IS DISTINCT FROM OLD.artifact_hashes
       OR NEW.confidence IS DISTINCT FROM OLD.confidence
       OR NEW.verification_status IS DISTINCT FROM OLD.verification_status
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.legacy_memory_id IS DISTINCT FROM OLD.legacy_memory_id
    THEN
        RAISE EXCEPTION 'memory content is immutable; create a superseding record';
    END IF;
    IF OLD.status IN ('superseded','revoked') AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'memory record % is already terminal', OLD.memory_id;
    END IF;
    NEW.updated_at := now();
    RETURN NEW;
END $$;
CREATE TRIGGER protect_memory_record
    BEFORE UPDATE OR DELETE ON knowledge.memory_records
    FOR EACH ROW EXECUTE FUNCTION knowledge.protect_memory_record();

CREATE FUNCTION knowledge.protect_memory_detail() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'memory detail rows are immutable';
END $$;
CREATE TRIGGER protect_memory_working
    BEFORE UPDATE OR DELETE ON knowledge.memory_working
    FOR EACH ROW EXECUTE FUNCTION knowledge.protect_memory_detail();
CREATE TRIGGER protect_memory_episodic
    BEFORE UPDATE OR DELETE ON knowledge.memory_episodic
    FOR EACH ROW EXECUTE FUNCTION knowledge.protect_memory_detail();
CREATE TRIGGER protect_memory_semantic
    BEFORE UPDATE OR DELETE ON knowledge.memory_semantic
    FOR EACH ROW EXECUTE FUNCTION knowledge.protect_memory_detail();
CREATE TRIGGER protect_memory_procedural
    BEFORE UPDATE OR DELETE ON knowledge.memory_procedural
    FOR EACH ROW EXECUTE FUNCTION knowledge.protect_memory_detail();
