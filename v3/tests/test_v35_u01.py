"""V3.5 U01: one task/run ledger with server-validated phase transitions.

The phase machine is enforced twice: the Python service rejects illegal
transitions before a write, and the PostgreSQL guard trigger rejects them even
for ad-hoc SQL.  Every write is CAS'd on the task revision and fenced by the
execution epoch, events are append-only and idempotent by key, and the legacy
V3.4 status values keep the repo-analysis/R5 API compatible.
"""

from __future__ import annotations

from uuid import UUID

import psycopg
import pytest

from mgar_v3.domain import DomainError
from mgar_v3.workbench.domain import (
    ALLOWED_TASK_PHASES,
    TERMINAL_PHASES,
    CreateWorkbenchTask,
)
from mgar_v3.workbench.service import WorkbenchService

REPO = "octo/example"


def _service(settings, engine) -> WorkbenchService:
    return WorkbenchService(engine, settings)


def _request(**overrides) -> CreateWorkbenchTask:
    body = {
        "repository_url": f"https://github.com/{REPO}",
        "mode": "audit",
        "task": "检查未知请求处理路径",
        "execution_mode": "fixture",
        "auto_start": False,
        "client_request_id": "u01-test",
    }
    body.update(overrides)
    return CreateWorkbenchTask(**body)


def _event(**overrides) -> dict:
    body = {
        "stage": "test",
        "status": "ok",
        "summary": "ledger probe",
        "payload": {},
        "kind": "test.probe",
        "idempotency_key": "probe-1",
    }
    body.update(overrides)
    return body


def _direct(engine, sql: str, params: tuple = ()) -> None:
    with engine.transaction() as conn:
        conn.execute(sql, params)


# -- phase machine ------------------------------------------------------
def test_the_python_phase_machine_matches_the_database_seed(engine) -> None:
    with engine.transaction() as conn:
        rows = conn.execute(
            "SELECT from_phase,to_phase FROM app.workbench_phase_transitions"
        ).fetchall()
    seeded = {(row["from_phase"], row["to_phase"]) for row in rows}
    python = {
        (source, target)
        for source, targets in ALLOWED_TASK_PHASES.items()
        for target in targets
    }
    assert seeded == python
    for phase in TERMINAL_PHASES:
        assert ALLOWED_TASK_PHASES[phase] == set()


def test_a_task_starts_queued_with_one_idempotent_created_event(settings, engine) -> None:
    service = _service(settings, engine)
    first = service.create(_request())
    second = service.create(_request())

    assert second["task_id"] == first["task_id"]
    assert first["phase"] == "queued"
    assert first["resume_phase"] == "queued"
    assert first["execution_epoch"] == 1
    assert first["transitions"] == []

    with engine.transaction() as conn:
        tasks = conn.execute("SELECT count(*) AS n FROM app.workbench_tasks").fetchone()
        events = conn.execute(
            "SELECT count(*) AS n FROM app.workbench_events "
            "WHERE task_id=%s AND kind='task.created'",
            (first["task_id"],),
        ).fetchone()
    assert tasks["n"] == 1
    assert events["n"] == 1


# -- the explicit forward sequence --------------------------------------
def test_fixture_audit_and_repair_walks_the_explicit_phase_sequence(
    settings, engine
) -> None:
    service = _service(settings, engine)
    payload = service.create(
        _request(
            mode="audit_and_repair",
            auto_start=True,
            client_request_id="u01-seq",
        )
    )
    task_id = payload["task_id"]

    transitions = service.store.transitions(task_id)
    assert [item["to_phase"] for item in transitions] == [
        "investigating",
        "suspected",
        "suspected",
        "reproduced",
        "repairing",
        "patch_verified",
        "waiting_approval",
    ]
    assert transitions[0]["from_phase"] == "queued"
    assert {item["actor"] for item in transitions} == {settings.local_actor}
    assert payload["phase"] == "waiting_approval"
    assert payload["status"] == "waiting_approval"
    assert payload["execution_epoch"] == 2

    links = payload["run_links"]
    assert [link["kind"] for link in links] == ["audit"]
    assert links[0]["run_id"] == payload["audit_run_id"]


# -- skip and fence gates ----------------------------------------------
def test_skipping_a_phase_is_rejected_by_the_service(settings, engine) -> None:
    service = _service(settings, engine)
    payload = service.create(_request())
    task = service.store.task(payload["task_id"])

    with pytest.raises(DomainError) as caught:
        service.store.transition(
            task["task_id"],
            to_phase="publishing",
            to_status="publishing",
            expected_revision=task["revision"],
            expected_epoch=task["execution_epoch"],
        )
    assert caught.value.code == "TASK_PHASE_INVALID"
    assert service.store.task(task["task_id"])["phase"] == "queued"


def test_skipping_a_phase_is_rejected_by_the_database_trigger(settings, engine) -> None:
    service = _service(settings, engine)
    task_id = service.create(_request())["task_id"]

    with pytest.raises(psycopg.Error) as caught:
        _direct(
            engine,
            "UPDATE app.workbench_tasks SET phase='pr_created',revision=revision+1 "
            "WHERE task_id=%s",
            (task_id,),
        )
    assert "WORKBENCH_PHASE_TRANSITION_DENIED" in str(caught.value)
    assert service.store.task(task_id)["phase"] == "queued"


def test_two_workers_with_the_same_revision_produce_one_transition(
    settings, engine
) -> None:
    service = _service(settings, engine)
    task_id = service.create(_request())["task_id"]
    first = service.store.task(task_id)
    second = service.store.task(task_id)

    service.store.transition(
        first["task_id"],
        to_phase="investigating",
        to_status="auditing",
        expected_revision=first["revision"],
        expected_epoch=first["execution_epoch"],
        actor="worker-a",
        event=_event(idempotency_key="worker-a"),
    )
    with pytest.raises(DomainError) as caught:
        service.store.transition(
            second["task_id"],
            to_phase="investigating",
            to_status="auditing",
            expected_revision=second["revision"],
            expected_epoch=second["execution_epoch"],
            actor="worker-b",
            event=_event(idempotency_key="worker-b"),
        )
    assert caught.value.code == "TASK_REVISION_CONFLICT"
    assert len(service.store.transitions(task_id)) == 1
    with engine.transaction() as conn:
        events = conn.execute(
            "SELECT count(*) AS n FROM app.workbench_events "
            "WHERE task_id=%s AND kind='test.probe'",
            (task_id,),
        ).fetchone()
    assert events["n"] == 1


def test_a_stale_epoch_cannot_write_after_the_fence(settings, engine) -> None:
    service = _service(settings, engine)
    task_id = service.create(_request())["task_id"]
    stale = service.store.task(task_id)
    service.store.bump_epoch(task_id, expected_epoch=stale["execution_epoch"])

    with pytest.raises(DomainError) as caught:
        service.store.transition(
            task_id,
            to_phase="investigating",
            to_status="auditing",
            expected_epoch=stale["execution_epoch"],
            actor="old-worker",
        )
    assert caught.value.code == "FENCED"
    assert service.store.task(task_id)["phase"] == "queued"
    assert service.store.transitions(task_id) == []


def _walk_to_waiting_approval(service: WorkbenchService, task_id) -> dict:
    task = service.store.task(task_id)
    for phase, status in [
        ("investigating", "auditing"),
        ("suspected", "ready"),
        ("reproduced", "ready"),
        ("repairing", "repairing"),
        ("patch_verified", "repairing"),
        ("waiting_approval", "waiting_approval"),
    ]:
        task = service.store.transition(
            task_id,
            to_phase=phase,
            to_status=status,
            expected_revision=task["revision"],
            expected_epoch=task["execution_epoch"],
            actor="worker",
        )
    return task


def test_publication_cannot_continue_after_cancel(settings, engine) -> None:
    service = _service(settings, engine)
    task_id = service.create(_request())["task_id"]
    _walk_to_waiting_approval(service, task_id)
    cancelled = service.cancel(task_id)
    assert cancelled["phase"] == "cancelled"

    current = service.store.task(task_id)
    with pytest.raises(DomainError) as caught:
        service.store.transition(
            task_id,
            to_phase="publishing",
            to_status="publishing",
            expected_revision=current["revision"],
            expected_epoch=current["execution_epoch"],
        )
    assert caught.value.code == "TASK_PHASE_INVALID"
    assert service.store.task(task_id)["phase"] == "cancelled"


def test_pr_created_cannot_be_reached_without_approval(settings, engine) -> None:
    service = _service(settings, engine)
    task_id = service.create(_request())["task_id"]
    task = _walk_to_waiting_approval(service, task_id)

    with pytest.raises(DomainError) as caught:
        service.store.transition(
            task_id,
            to_phase="pr_created",
            to_status="completed",
            expected_revision=task["revision"],
            expected_epoch=task["execution_epoch"],
        )
    assert caught.value.code == "TASK_PHASE_INVALID"
    assert service.store.task(task_id)["phase"] == "waiting_approval"


def test_request_id_reuse_with_a_different_payload_is_rejected(settings, engine) -> None:
    service = _service(settings, engine)
    service.create(_request(client_request_id="u01-reuse"))

    with pytest.raises(DomainError) as caught:
        service.create(_request(client_request_id="u01-reuse", task="另一个任务"))
    assert caught.value.code == "REQUEST_ID_REUSED"


# -- append-only, idempotency, identity --------------------------------
def test_events_are_idempotent_by_key(settings, engine) -> None:
    service = _service(settings, engine)
    task_id = service.create(_request())["task_id"]

    assert service.store.append_events(task_id, [_event()])
    assert service.store.append_events(task_id, [_event()]) == []
    assert service.store.append_events(task_id, [_event(idempotency_key="probe-2")])

    events = [item for item in service.store.events(task_id) if item["kind"] == "test.probe"]
    assert [item["idempotency_key"] for item in events] == ["probe-1", "probe-2"]
    assert {item["execution_epoch"] for item in events} == {1}


def test_events_and_transitions_are_append_only(settings, engine) -> None:
    service = _service(settings, engine)
    task_id = service.create(_request())["task_id"]
    service.store.append_events(task_id, [_event()])
    task = service.store.task(task_id)
    service.store.transition(
        task_id,
        to_phase="investigating",
        to_status="auditing",
        expected_revision=task["revision"],
        expected_epoch=task["execution_epoch"],
        actor="worker",
    )

    for statement in (
        "UPDATE app.workbench_events SET summary='rewritten' WHERE task_id=%s",
        "DELETE FROM app.workbench_events WHERE task_id=%s",
        "UPDATE app.workbench_transitions SET reason='rewritten' WHERE task_id=%s",
        "DELETE FROM app.workbench_transitions WHERE task_id=%s",
    ):
        with pytest.raises(psycopg.Error) as caught:
            _direct(engine, statement, (task_id,))
        assert "WORKBENCH_APPEND_ONLY" in str(caught.value)

    assert len(service.store.events(task_id)) == 3
    assert len(service.store.transitions(task_id)) == 1


def test_identity_columns_cannot_be_rewritten(settings, engine) -> None:
    service = _service(settings, engine)
    task_id = service.create(_request())["task_id"]

    with pytest.raises(psycopg.Error) as caught:
        _direct(
            engine,
            "UPDATE app.workbench_tasks SET mode='issue_repair',revision=revision+1 "
            "WHERE task_id=%s",
            (task_id,),
        )
    assert "WORKBENCH_IDENTITY_IMMUTABLE" in str(caught.value)


def test_revision_must_advance_by_exactly_one(settings, engine) -> None:
    service = _service(settings, engine)
    task_id = service.create(_request())["task_id"]

    with pytest.raises(psycopg.Error) as caught:
        _direct(
            engine,
            "UPDATE app.workbench_tasks SET revision=revision+2 WHERE task_id=%s",
            (task_id,),
        )
    assert "WORKBENCH_REVISION_CAS" in str(caught.value)


# -- controls and the run ledger ---------------------------------------
def test_pause_and_resume_restore_the_resume_phase(settings, engine) -> None:
    service = _service(settings, engine)
    created = service.create(_request(client_request_id="u01-pause"))
    task_id = created["task_id"]
    task = service.store.task(task_id)
    task = service.store.transition(
        task_id,
        to_phase="investigating",
        to_status="auditing",
        expected_revision=task["revision"],
        expected_epoch=task["execution_epoch"],
        actor="worker",
    )
    assert task["phase"] == "investigating"
    epoch = task["execution_epoch"]

    paused = service.pause(task_id)
    assert paused["phase"] == "paused"
    assert paused["resume_phase"] == "investigating"
    assert paused["status"] == "paused"
    assert paused["execution_epoch"] == epoch + 1

    resumed = service.resume(task_id)
    assert resumed["phase"] == "investigating"
    assert resumed["status"] == "queued"
    assert resumed["resume_phase"] == "queued"
    assert resumed["execution_epoch"] == epoch + 2


def test_one_task_links_audit_and_verifier_runs(settings, engine) -> None:
    service = _service(settings, engine)
    ready = service.create(_request(auto_start=True, client_request_id="u01-runs"))
    task_id = ready["task_id"]
    audit_run_id = ready["audit_run_id"]
    assert isinstance(audit_run_id, UUID)

    service.store.link_run(task_id, "verifier", audit_run_id)
    view = service.view(task_id)
    assert [(link["kind"], link["run_id"]) for link in view["run_links"]] == [
        ("audit", audit_run_id),
        ("verifier", audit_run_id),
    ]
    assert view["audit_run_id"] == audit_run_id
    assert view["verifier_run_id"] == audit_run_id
    assert view["repair_run_id"] is None
