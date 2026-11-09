"""V3.3 M01: unified memory schema, scoping and lifecycle over real PostgreSQL."""

import hashlib
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from pydantic import ValidationError

from mgar_v3.domain import DomainError
from mgar_v3.memory import (
    EpisodicDetail,
    MemoryDraft,
    MemoryStore,
    ProceduralDetail,
    SemanticDetail,
    WorkingDetail,
)
from mgar_v3.settings import Settings
from mgar_v3.store.migrate import migrate

COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40


def _conn(settings: Settings) -> psycopg.Connection:
    return psycopg.connect(settings.database_url.get_secret_value())


def _artifact(settings: Settings, name: str) -> str:
    content_hash = hashlib.sha256(f"memory-fixture:{name}".encode()).hexdigest()
    with _conn(settings) as conn:
        conn.execute(
            "INSERT INTO app.artifacts(content_hash,byte_count) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING",
            (content_hash, len(name)),
        )
    return content_hash


def _scope(
    settings: Settings,
    *,
    project_id: str | None = None,
    repository: str | None = None,
    actor: str | None = None,
) -> tuple[UUID, UUID]:
    project = settings.project_id if project_id is None else project_id
    repo = (settings.repository if repository is None else repository).lower()
    who = settings.local_actor if actor is None else actor
    thread_id, mandate_id, ticket_id, run_id = uuid4(), uuid4(), uuid4(), uuid4()
    with _conn(settings) as conn:
        conn.execute(
            "INSERT INTO app.threads(thread_id,project_id,repository,external_key) "
            "VALUES (%s,%s,%s,%s)",
            (thread_id, project, repo, f"thread:{thread_id}"),
        )
        conn.execute(
            "INSERT INTO app.mandates(mandate_id,project_id,actor,scope,policy_version,"
            "execution_mode) VALUES (%s,%s,%s,%s,%s,%s)",
            (mandate_id, project, who, Jsonb({}), "r1-explicit-command-v1", "fixture"),
        )
        conn.execute(
            "INSERT INTO app.tickets(ticket_id,mandate_id,thread_id,status,outcome) "
            "VALUES (%s,%s,%s,'ended','succeeded')",
            (ticket_id, mandate_id, thread_id),
        )
        conn.execute(
            "INSERT INTO app.runs(run_id,ticket_id,attempt_no,input_revision,"
            "graph_version,execution_mode,status,question) VALUES "
            "(%s,%s,1,1,%s,'fixture','succeeded',%s)",
            (run_id, ticket_id, "r1-quotes-v1", "memory schema fixture"),
        )
    return ticket_id, run_id


def _evidence(
    settings: Settings,
    run_id: UUID,
    artifact_hash: str,
    *,
    project_id: str | None = None,
) -> UUID:
    evidence_id = uuid4()
    with _conn(settings) as conn:
        conn.execute(
            "INSERT INTO app.evidence(evidence_id,run_id,project_id,source_uri,"
            "source_version,content_hash,start_line,end_line,tool_call_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,1,2,%s)",
            (
                evidence_id,
                run_id,
                settings.project_id if project_id is None else project_id,
                "repo://src/app.py",
                "fixture-v1",
                artifact_hash,
                uuid4(),
            ),
        )
    return evidence_id


def _legacy(
    settings: Settings,
    *,
    ticket_id: UUID,
    run_id: UUID,
    artifacts: dict[str, str],
    evidence_ids: list[UUID] | None = None,
    revoked: bool = False,
) -> UUID:
    memory_id = uuid4()
    with _conn(settings) as conn:
        conn.execute(
            "INSERT INTO knowledge.memories(memory_id,project_id,repository,actor,"
            "ticket_id,run_id,report_hash,summary_hash,evidence_ids,confirmed_by,"
            "revoked_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                memory_id,
                settings.project_id,
                settings.repository.lower(),
                settings.local_actor,
                ticket_id,
                run_id,
                artifacts["report"],
                artifacts["summary"],
                Jsonb([str(item) for item in evidence_ids or []]),
                settings.local_actor,
                datetime.now(UTC) if revoked else None,
            ),
        )
    return memory_id


def _draft(
    settings: Settings,
    artifacts: dict[str, str],
    *,
    kind: str = "semantic",
    scope: str = "repository",
    subject: str = "duplicate-count",
    summary_hash: str | None = None,
    commit: str | None = None,
    actor: str | None = None,
    detail: object | None = None,
    evidence_ids: list[UUID] | None = None,
    artifact_hashes: list[str] | None = None,
    confidence: float = 0.8,
    verification_status: str = "supported",
    project_id: str | None = None,
    repository: str | None = None,
) -> MemoryDraft:
    return MemoryDraft(
        project_id=settings.project_id if project_id is None else project_id,
        repository=settings.repository if repository is None else repository,
        commit=commit,
        actor=settings.local_actor if actor is None else actor,
        memory_kind=kind,  # type: ignore[arg-type]
        scope=scope,  # type: ignore[arg-type]
        subject_key=subject,
        summary_hash=artifacts["summary"] if summary_hash is None else summary_hash,
        evidence_ids=list(evidence_ids or []),
        artifact_hashes=list(artifact_hashes or []),
        confidence=confidence,
        verification_status=verification_status,  # type: ignore[arg-type]
        detail=detail
        or SemanticDetail(
            subject="duplicate-count", predicate="appears_in", object_value="paid-events"
        ),
    )


@pytest.fixture
def store(settings: Settings) -> MemoryStore:
    return MemoryStore(settings)


@pytest.fixture
def artifacts(settings: Settings) -> dict[str, str]:
    names = ("summary", "summary-v2", "report", "recipe")
    return {name: _artifact(settings, name) for name in names}


def test_migration_is_additive_and_keeps_the_legacy_table(settings: Settings):
    assert migrate(settings.database_url.get_secret_value()) == []
    with _conn(settings) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT table_name FROM information_schema.tables WHERE table_schema='knowledge'"
            )
        }
        assert {
            "memories",
            "memory_records",
            "memory_working",
            "memory_episodic",
            "memory_semantic",
            "memory_procedural",
        } <= tables
        applied = {row[0] for row in conn.execute("SELECT name FROM app.schema_migrations")}
        assert "0021_memory_schema.sql" in applied
        legacy_columns = {
            row[0]
            for row in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE "
                "table_schema='knowledge' AND table_name='memories'"
            )
        }
        assert {"memory_id", "summary_hash", "evidence_ids", "revoked_at"} <= legacy_columns


def test_four_kinds_share_one_identity_model(
    settings: Settings, artifacts: dict[str, str], store: MemoryStore
):
    ticket_id, run_id = _scope(settings)
    now = datetime.now(UTC)
    details = [
        WorkingDetail(run_id=run_id, expires_at=now + timedelta(hours=1)),
        EpisodicDetail(
            ticket_id=ticket_id,
            run_id=run_id,
            event_kind="investigation_completed",
            occurred_at=now,
        ),
        SemanticDetail(
            subject="duplicate-count", predicate="appears_in", object_value="paid-events"
        ),
        ProceduralDetail(task_key="dedupe", recipe_hash=artifacts["recipe"], success_count=2),
    ]
    records = [
        store.create(
            _draft(
                settings,
                artifacts,
                kind=kind,
                subject="duplicate-count",
                detail=detail,
            )
        )
        for kind, detail in zip(
            ("working", "episodic", "semantic", "procedural"), details, strict=True
        )
    ]
    assert [record.memory_kind for record in records] == [
        "working",
        "episodic",
        "semantic",
        "procedural",
    ]
    assert {record.subject_key for record in records} == {"duplicate-count"}
    assert all(record.repository == settings.repository.lower() for record in records)
    assert all(record.scope == "repository" for record in records)
    assert isinstance(records[0].detail, WorkingDetail)
    assert isinstance(records[1].detail, EpisodicDetail)
    assert isinstance(records[2].detail, SemanticDetail)
    assert isinstance(records[3].detail, ProceduralDetail)
    assert {record.memory_id for record in store.recall()} == {
        record.memory_id for record in records
    }


def test_commit_scope_is_explicit_and_filterable(
    settings: Settings, artifacts: dict[str, str], store: MemoryStore
):
    record = store.create(_draft(settings, artifacts, scope="commit", commit=COMMIT))
    assert record.scope == "commit" and record.scope_key == COMMIT
    current = store.current(
        memory_kind="semantic", scope="commit", subject_key="duplicate-count", commit=COMMIT
    )
    assert current is not None and current.memory_id == record.memory_id
    assert (
        store.current(
            memory_kind="semantic",
            scope="commit",
            subject_key="duplicate-count",
            commit=OTHER_COMMIT,
        )
        is None
    )
    assert [item.memory_id for item in store.recall(commit=COMMIT)] == [record.memory_id]
    assert store.recall(commit=OTHER_COMMIT) == []
    with pytest.raises(ValidationError):
        _draft(settings, artifacts, scope="commit", commit=None)
    with pytest.raises(ValidationError):
        _draft(settings, artifacts, scope="repository", commit=COMMIT)
    with pytest.raises(DomainError) as exc:
        store.recall(scope="commit")
    assert exc.value.code == "MEMORY_SCOPE_INVALID"


def test_cross_repository_and_project_reads_are_rejected(
    settings: Settings, artifacts: dict[str, str], store: MemoryStore
):
    record = store.create(_draft(settings, artifacts))
    other_repository = MemoryStore(
        settings.model_copy(update={"repository": "example/other-repository"})
    )
    with pytest.raises(DomainError) as exc:
        other_repository.get(record.memory_id)
    assert exc.value.code == "MEMORY_NOT_FOUND"
    assert other_repository.recall() == []
    assert other_repository.legacy() == []
    other_project = MemoryStore(settings.model_copy(update={"project_id": "other-project"}))
    with pytest.raises(DomainError) as exc:
        other_project.get(record.memory_id)
    assert exc.value.code == "MEMORY_NOT_FOUND"
    with pytest.raises(DomainError) as exc:
        store.create(_draft(settings, artifacts, repository="example/other-repository"))
    assert exc.value.code == "MEMORY_SCOPE_DENIED"
    with pytest.raises(DomainError) as exc:
        store.create(_draft(settings, artifacts, project_id="other-project"))
    assert exc.value.code == "MEMORY_SCOPE_DENIED"


def test_cross_actor_reads_are_rejected(
    settings: Settings, artifacts: dict[str, str]
):
    settings.allowed_actors = ["owner", "outsider"]
    store = MemoryStore(settings)
    record = store.create(_draft(settings, artifacts))
    with pytest.raises(DomainError) as exc:
        store.get(record.memory_id, actor="outsider")
    assert exc.value.code == "MEMORY_NOT_FOUND"
    assert store.recall(actor="outsider") == []
    with pytest.raises(DomainError) as exc:
        store.get(record.memory_id, actor="stranger")
    assert exc.value.code == "MEMORY_ACTOR_DENIED"
    with pytest.raises(DomainError) as exc:
        store.create(_draft(settings, artifacts, subject="other"), actor="outsider")
    assert exc.value.code == "MEMORY_ACTOR_DENIED"
    ticket_id, run_id = _scope(settings)
    legacy_id = _legacy(
        settings,
        ticket_id=ticket_id,
        run_id=run_id,
        artifacts=artifacts,
    )
    with pytest.raises(DomainError) as exc:
        store.get(legacy_id, actor="outsider")
    assert exc.value.code == "MEMORY_NOT_FOUND"


def test_legacy_memory_remains_accessible_and_can_be_imported(
    settings: Settings, artifacts: dict[str, str], store: MemoryStore
):
    ticket_id, run_id = _scope(settings)
    evidence_id = _evidence(settings, run_id, artifacts["report"])
    legacy_id = _legacy(
        settings,
        ticket_id=ticket_id,
        run_id=run_id,
        artifacts=artifacts,
        evidence_ids=[evidence_id],
    )
    legacy = store.legacy()
    assert [record.memory_id for record in legacy] == [legacy_id]
    record = store.get(legacy_id)
    assert record.legacy is True and record.status == "active"
    assert record.evidence_ids == [evidence_id]
    assert record.artifact_hashes == [artifacts["report"]]
    assert record.verification_status == "verified"
    imported = store.import_legacy(legacy_id)
    assert imported.memory_id != legacy_id
    assert imported.legacy is True
    assert imported.evidence_ids == [evidence_id]
    assert store.import_legacy(legacy_id).memory_id == imported.memory_id
    assert store.get(legacy_id).memory_id == imported.memory_id
    with _conn(settings) as conn:
        row = conn.execute(
            "SELECT revoked_at,summary_hash,evidence_ids FROM knowledge.memories "
            "WHERE memory_id=%s",
            (legacy_id,),
        ).fetchone()
        assert row is not None
        assert row[0] is None and row[1] == artifacts["summary"]
        assert row[2] == [str(evidence_id)]
        count = conn.execute("SELECT count(*) FROM knowledge.memories").fetchone()
        assert count is not None and count[0] == 1


def test_revoked_legacy_memory_is_not_visible(
    settings: Settings, artifacts: dict[str, str], store: MemoryStore
):
    ticket_id, run_id = _scope(settings)
    legacy_id = _legacy(
        settings,
        ticket_id=ticket_id,
        run_id=run_id,
        artifacts=artifacts,
        revoked=True,
    )
    assert store.legacy() == []
    with pytest.raises(DomainError) as exc:
        store.get(legacy_id)
    assert exc.value.code == "MEMORY_NOT_FOUND"
    with pytest.raises(DomainError) as exc:
        store.import_legacy(legacy_id)
    assert exc.value.code == "MEMORY_LEGACY_NOT_FOUND"


def test_history_is_not_silently_overwritten(
    settings: Settings, artifacts: dict[str, str], store: MemoryStore
):
    first = store.create(_draft(settings, artifacts, confidence=0.6))
    with pytest.raises(DomainError) as exc:
        store.create(_draft(settings, artifacts, confidence=0.7))
    assert exc.value.code == "MEMORY_CONFLICT"
    second = store.supersede(
        first.memory_id,
        _draft(settings, artifacts, summary_hash=artifacts["summary-v2"], confidence=0.9),
    )
    assert second.memory_id != first.memory_id
    with pytest.raises(DomainError) as exc:
        store.get(first.memory_id)
    assert exc.value.code == "MEMORY_NOT_FOUND"
    old = store.get(first.memory_id, include_inactive=True)
    assert old.status == "superseded" and old.superseded_by == second.memory_id
    history = store.history(
        memory_kind="semantic", scope="repository", subject_key="duplicate-count"
    )
    assert [record.status for record in history] == ["superseded", "active"]
    assert [record.memory_id for record in history] == [first.memory_id, second.memory_id]
    with pytest.raises(psycopg.errors.RaiseException):
        with _conn(settings) as conn:
            conn.execute(
                "UPDATE knowledge.memory_records SET confidence=0.1 WHERE memory_id=%s",
                (second.memory_id,),
            )
    with pytest.raises(psycopg.errors.RaiseException):
        with _conn(settings) as conn:
            conn.execute(
                "DELETE FROM knowledge.memory_records WHERE memory_id=%s",
                (second.memory_id,),
            )
    with pytest.raises(psycopg.errors.RaiseException):
        with _conn(settings) as conn:
            conn.execute(
                "UPDATE knowledge.memory_semantic SET object_value='rewritten' "
                "WHERE memory_id=%s",
                (second.memory_id,),
            )


def test_supersede_requires_the_same_identity_and_a_live_record(
    settings: Settings, artifacts: dict[str, str], store: MemoryStore
):
    first = store.create(_draft(settings, artifacts))
    with pytest.raises(DomainError) as exc:
        store.supersede(first.memory_id, _draft(settings, artifacts, subject="other-subject"))
    assert exc.value.code == "MEMORY_IDENTITY_MISMATCH"
    revoked = store.revoke(first.memory_id, "superseded by policy")
    assert revoked.status == "revoked" and revoked.revoked_reason == "superseded by policy"
    with pytest.raises(DomainError) as exc:
        store.supersede(first.memory_id, _draft(settings, artifacts))
    assert exc.value.code == "MEMORY_TRANSITION_INVALID"
    with pytest.raises(DomainError) as exc:
        store.revoke(first.memory_id, "again")
    assert exc.value.code == "MEMORY_TRANSITION_INVALID"
    with pytest.raises(DomainError) as exc:
        store.revoke(first.memory_id, "   ")
    assert exc.value.code == "MEMORY_REASON_REQUIRED"


def test_revoke_hides_recall_and_keeps_history(
    settings: Settings, artifacts: dict[str, str], store: MemoryStore
):
    record = store.create(_draft(settings, artifacts))
    revoked = store.revoke(record.memory_id, "source was withdrawn")
    assert revoked.status == "revoked" and revoked.revoked_at is not None
    assert store.recall() == []
    with pytest.raises(DomainError):
        store.get(record.memory_id)
    kept = store.get(record.memory_id, include_inactive=True)
    assert kept.status == "revoked" and kept.revoked_reason == "source was withdrawn"
    history = store.history(
        memory_kind="semantic", scope="repository", subject_key="duplicate-count"
    )
    assert [item.status for item in history] == ["revoked"]


def test_stale_memory_is_retained_but_not_recalled(
    settings: Settings, artifacts: dict[str, str], store: MemoryStore
):
    record = store.create(_draft(settings, artifacts))
    stale = store.mark_stale(record.memory_id, "commit moved")
    assert stale.status == "stale" and stale.stale_at is not None
    assert store.get(record.memory_id).status == "stale"
    assert store.recall() == []
    current = store.current(
        memory_kind="semantic", scope="repository", subject_key="duplicate-count"
    )
    assert current is not None and current.status == "stale"
    replacement = store.supersede(
        record.memory_id,
        _draft(settings, artifacts, summary_hash=artifacts["summary-v2"]),
    )
    old = store.get(record.memory_id, include_inactive=True)
    assert old.status == "superseded" and old.stale_reason == "commit moved"
    assert old.superseded_by == replacement.memory_id
    with pytest.raises(DomainError) as exc:
        store.mark_stale(record.memory_id, "too late")
    assert exc.value.code == "MEMORY_TRANSITION_INVALID"


def test_working_memory_expiry_is_excluded_from_recall(
    settings: Settings, artifacts: dict[str, str], store: MemoryStore
):
    _, run_id = _scope(settings)
    now = datetime.now(UTC)
    expired = store.create(
        _draft(
            settings,
            artifacts,
            kind="working",
            subject="expired-focus",
            detail=WorkingDetail(run_id=run_id, expires_at=now - timedelta(minutes=1)),
        )
    )
    live = store.create(
        _draft(
            settings,
            artifacts,
            kind="working",
            subject="live-focus",
            detail=WorkingDetail(run_id=run_id, expires_at=now + timedelta(minutes=5)),
        )
    )
    assert [record.memory_id for record in store.recall(memory_kinds=["working"])] == [
        live.memory_id
    ]
    assert store.get(expired.memory_id).status == "active"
    assert (
        store.current(memory_kind="working", scope="repository", subject_key="expired-focus")
        is None
    )


def test_references_evidence_and_verification_are_checked(
    settings: Settings, artifacts: dict[str, str], store: MemoryStore
):
    ticket_id, run_id = _scope(settings)
    evidence_id = _evidence(settings, run_id, artifacts["report"])
    with pytest.raises(DomainError) as exc:
        store.create(_draft(settings, artifacts, summary_hash="f" * 64))
    assert exc.value.code == "MEMORY_REFERENCE_INVALID"
    with pytest.raises(DomainError) as exc:
        store.create(_draft(settings, artifacts, evidence_ids=[uuid4()]))
    assert exc.value.code == "MEMORY_REFERENCE_INVALID"
    with pytest.raises(DomainError) as exc:
        store.create(_draft(settings, artifacts, verification_status="verified"))
    assert exc.value.code == "MEMORY_EVIDENCE_REQUIRED"
    verified = store.create(
        _draft(
            settings,
            artifacts,
            evidence_ids=[evidence_id],
            verification_status="verified",
            confidence=0.95,
        )
    )
    assert verified.verification_status == "verified"
    with pytest.raises(DomainError) as exc:
        store.create(
            _draft(
                settings,
                artifacts,
                kind="procedural",
                detail=ProceduralDetail(task_key="dedupe", recipe_hash="e" * 64),
            )
        )
    assert exc.value.code == "MEMORY_REFERENCE_INVALID"
    with pytest.raises(DomainError) as exc:
        store.create(
            _draft(
                settings,
                artifacts,
                kind="episodic",
                detail=EpisodicDetail(
                    ticket_id=ticket_id,
                    run_id=uuid4(),
                    event_kind="investigation_completed",
                    occurred_at=datetime.now(UTC),
                ),
            )
        )
    assert exc.value.code == "MEMORY_REFERENCE_INVALID"
    other_ticket, other_run = _scope(settings, repository="example/other-repository")
    other_evidence = _evidence(settings, other_run, artifacts["report"])
    with pytest.raises(DomainError) as exc:
        store.create(
            _draft(
                settings,
                artifacts,
                kind="episodic",
                detail=EpisodicDetail(
                    ticket_id=ticket_id,
                    run_id=other_run,
                    event_kind="investigation_completed",
                    occurred_at=datetime.now(UTC),
                ),
            )
        )
    assert exc.value.code == "MEMORY_SCOPE_DENIED"
    with pytest.raises(DomainError) as exc:
        store.create(_draft(settings, artifacts, evidence_ids=[other_evidence]))
    assert exc.value.code == "MEMORY_SCOPE_DENIED"
    with pytest.raises(ValidationError):
        _draft(settings, artifacts, confidence=1.5)
    with pytest.raises(ValidationError):
        _draft(settings, artifacts, verification_status="trust_me")
