CREATE TABLE knowledge.documents (
    document_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    repository text NOT NULL,
    owner text NOT NULL,
    readers text[] NOT NULL DEFAULT '{}',
    source_uri text NOT NULL,
    title text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN ('active','revoked')),
    acl_revision bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(project_id,repository,owner,source_uri)
);
CREATE TABLE knowledge.document_versions (
    version_id uuid PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES knowledge.documents,
    source_version text NOT NULL,
    raw_hash text NOT NULL REFERENCES app.artifacts,
    media_type text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(document_id,source_version)
);
CREATE TABLE knowledge.chunks (
    chunk_id uuid PRIMARY KEY,
    version_id uuid NOT NULL REFERENCES knowledge.document_versions,
    content_hash text NOT NULL REFERENCES app.artifacts,
    text_content text NOT NULL,
    locator jsonb NOT NULL,
    terms jsonb NOT NULL,
    token_count integer NOT NULL,
    UNIQUE(version_id,content_hash,locator)
);
CREATE TABLE knowledge.snapshots (
    snapshot_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    repository text NOT NULL,
    actor text NOT NULL,
    status text NOT NULL CHECK (status IN ('building','ready','failed')),
    config jsonb NOT NULL,
    config_hash text NOT NULL,
    dimension integer NOT NULL CHECK (dimension BETWEEN 1 AND 2048),
    manifest_hash text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE knowledge.snapshot_chunks (
    snapshot_id uuid NOT NULL REFERENCES knowledge.snapshots,
    chunk_id uuid NOT NULL REFERENCES knowledge.chunks,
    embedding vector NOT NULL,
    PRIMARY KEY(snapshot_id,chunk_id)
);
CREATE TABLE knowledge.active_snapshots (
    project_id text NOT NULL,
    repository text NOT NULL,
    actor text NOT NULL,
    snapshot_id uuid NOT NULL REFERENCES knowledge.snapshots,
    PRIMARY KEY(project_id,repository,actor)
);
CREATE TABLE knowledge.ingestion_jobs (
    job_id uuid PRIMARY KEY,
    version_id uuid NOT NULL REFERENCES knowledge.document_versions,
    actor text NOT NULL,
    project_id text NOT NULL,
    snapshot_id uuid NOT NULL REFERENCES knowledge.snapshots,
    expected_snapshot_id uuid,
    status text NOT NULL CHECK (status IN ('queued','indexing','succeeded','failed')),
    lease_token uuid,
    reason_code text,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX one_ingestion_version_config ON knowledge.ingestion_jobs(version_id,actor,snapshot_id);
CREATE TABLE knowledge.search_traces (
    trace_id uuid PRIMARY KEY,
    run_id uuid REFERENCES app.runs,
    actor text NOT NULL,
    snapshot_id uuid NOT NULL REFERENCES knowledge.snapshots,
    request_hash text NOT NULL,
    scope_hash text NOT NULL,
    config_hash text NOT NULL,
    cache_state text NOT NULL,
    results jsonb NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE app.tool_grants (
    token_hash text PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES app.runs,
    task_id text NOT NULL,
    role text NOT NULL CHECK (role IN ('knowledge','repo','data')),
    execution_epoch bigint NOT NULL,
    actor text NOT NULL,
    scope jsonb NOT NULL,
    remaining_calls integer NOT NULL CHECK (remaining_calls>=0),
    expires_at timestamptz NOT NULL,
    UNIQUE(run_id,task_id,execution_epoch)
);
CREATE TABLE app.r4_reads (
    evidence_id uuid PRIMARY KEY REFERENCES app.evidence,
    role text NOT NULL,
    task_id text NOT NULL,
    origin jsonb NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(task_id,evidence_id)
);
CREATE TABLE app.r4_context (
    run_id uuid NOT NULL REFERENCES app.runs,
    input_revision bigint NOT NULL,
    knowledge_snapshot_ids jsonb NOT NULL DEFAULT '[]',
    profile jsonb NOT NULL,
    profile_hash text NOT NULL,
    tool_calls_used integer NOT NULL DEFAULT 0,
    PRIMARY KEY(run_id,input_revision)
);
ALTER TABLE app.runs ADD COLUMN r4_requested_snapshots jsonb NOT NULL DEFAULT '[]';
CREATE TABLE app.resolutions (
    run_id uuid NOT NULL REFERENCES app.runs,
    input_revision bigint NOT NULL,
    report_hash text NOT NULL REFERENCES app.artifacts,
    payload jsonb NOT NULL,
    verdict jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(run_id,input_revision)
);
CREATE TABLE knowledge.context_edges (
    edge_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    repository text NOT NULL,
    actor text NOT NULL,
    source_kind text NOT NULL,
    source_id uuid NOT NULL,
    target_kind text NOT NULL,
    target_id uuid NOT NULL,
    relation text NOT NULL CHECK (relation IN ('triggered_by','part_of','attempt_of','derived_from','cites','supersedes','related_to')),
    run_id uuid REFERENCES app.runs,
    UNIQUE(source_kind,source_id,target_kind,target_id,relation)
);
CREATE TABLE knowledge.memories (
    memory_id uuid PRIMARY KEY,
    project_id text NOT NULL,
    repository text NOT NULL,
    actor text NOT NULL,
    ticket_id uuid NOT NULL REFERENCES app.tickets,
    run_id uuid NOT NULL REFERENCES app.runs,
    report_hash text NOT NULL REFERENCES app.artifacts,
    summary_hash text NOT NULL REFERENCES app.artifacts,
    evidence_ids jsonb NOT NULL,
    confirmed_by text NOT NULL,
    confirmed_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    UNIQUE(ticket_id,report_hash,summary_hash)
);
CREATE FUNCTION knowledge.protect_r4_snapshot() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status='ready' THEN RAISE EXCEPTION 'ready knowledge snapshots are immutable'; END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER immutable_ready_snapshot BEFORE UPDATE ON knowledge.snapshots FOR EACH ROW EXECUTE FUNCTION knowledge.protect_r4_snapshot();
CREATE FUNCTION knowledge.protect_r4_membership() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS(SELECT 1 FROM knowledge.snapshots WHERE snapshot_id=COALESCE(NEW.snapshot_id,OLD.snapshot_id) AND status='ready')
    THEN RAISE EXCEPTION 'ready knowledge membership is immutable'; END IF;
    RETURN COALESCE(NEW,OLD);
END $$;
CREATE TRIGGER immutable_snapshot_membership BEFORE INSERT OR UPDATE OR DELETE ON knowledge.snapshot_chunks FOR EACH ROW EXECUTE FUNCTION knowledge.protect_r4_membership();
CREATE TABLE app.expert_results (
    run_id uuid NOT NULL REFERENCES app.runs,
    task_id text NOT NULL,
    input_revision bigint NOT NULL,
    role text NOT NULL,
    result jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(run_id,task_id)
);
