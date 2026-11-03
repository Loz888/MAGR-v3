from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from mgar_v3.domain import Model, digest

Kind = Literal[
    "no_progress",
    "waiting_input",
    "waiting_approval",
    "outcome_unknown",
    "heartbeat_expired",
    "mailbox_backlog",
    "router_backlog",
    "outbox_backlog",
    "dependency_unavailable",
]
VERSION: Literal["r6-observe-v1"] = "r6-observe-v1"


class Observation(Model):
    run_id: UUID
    execution_epoch: int
    status: str
    progress_at: datetime
    progress_cursor: int
    unknown_action_id: UUID | None
    unknown_call_id: UUID | None = None
    heartbeat_expires_at: datetime | None = None
    mailbox_pending: int = 0


class Snapshot(Model):
    version: Literal["r6-observe-v1"] = VERSION
    mode: Literal["observe"] = "observe"
    sampled_at: datetime
    items: list[Observation]
    truncated: bool = False
    system: dict[str, str | int] = Field(default_factory=dict)
    system_cursors: dict[str, str] = Field(default_factory=dict)
    project_id: str = ""
    repository: str = ""


class RecoveryInput(Model):
    run_id: UUID | None
    execution_epoch: Annotated[int, Field(strict=True, ge=0)]
    kind: Kind
    observation_key: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class Decision(Model):
    decision: Literal["wait", "escalate", "pause", "cancel"]


def detect(snapshot: Snapshot) -> list[RecoveryInput]:
    result = []
    for item in snapshot.items:
        age = (snapshot.sampled_at - item.progress_at).total_seconds()
        kind: Kind | None = None
        if item.unknown_action_id or item.unknown_call_id:
            kind = "outcome_unknown"
        elif (
            item.status == "running"
            and item.heartbeat_expires_at is not None
            and (item.heartbeat_expires_at <= snapshot.sampled_at)
        ):
            kind = "heartbeat_expired"
        elif item.mailbox_pending >= 16:
            kind = "mailbox_backlog"
        elif item.status in {"running", "queued"} and age >= 120:
            kind = "no_progress"
        elif item.status in {"waiting_input", "waiting_approval"} and age >= 3600:
            kind = "waiting_input" if item.status == "waiting_input" else "waiting_approval"
        if kind:
            result.append(
                RecoveryInput(
                    run_id=item.run_id,
                    execution_epoch=item.execution_epoch,
                    kind=kind,
                    observation_key=digest(
                        {
                            "version": VERSION,
                            "run": str(item.run_id),
                            "epoch": item.execution_epoch,
                            "kind": kind,
                            "marker": str(item.unknown_action_id or item.unknown_call_id)
                            if item.unknown_action_id or item.unknown_call_id
                            else str(item.heartbeat_expires_at)
                            if kind == "heartbeat_expired"
                            else [item.mailbox_pending, item.progress_cursor]
                            if kind == "mailbox_backlog"
                            else item.progress_cursor,
                        }
                    ),
                )
            )
    for label, value in snapshot.system.items():
        kind = None
        if label in {"router_backlog", "outbox_backlog"} and isinstance(value, int) and value >= 16:
            kind = "router_backlog" if label == "router_backlog" else "outbox_backlog"
        elif label.startswith("dependency:") and value == "unavailable":
            kind = "dependency_unavailable"
        elif label == "router_usage_unknown" and isinstance(value, int) and value > 0:
            kind = "outcome_unknown"
        if kind:
            result.append(
                RecoveryInput(
                    run_id=None,
                    execution_epoch=0,
                    kind=kind,
                    observation_key=digest(
                        {
                            "version": VERSION,
                            "system": label,
                            "value": value,
                            "project": snapshot.project_id,
                            "repository": snapshot.repository,
                            "cursor": snapshot.system_cursors.get(label, ""),
                        }
                    ),
                )
            )
    return result
