from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from mgar_v3.domain import Model, digest

Kind = Literal["no_progress", "waiting_input", "waiting_approval", "outcome_unknown"]
VERSION: Literal["r6-observe-v1"] = "r6-observe-v1"


class Observation(Model):
    run_id: UUID
    execution_epoch: int
    status: str
    progress_at: datetime
    progress_cursor: int
    unknown_action_id: UUID | None


class Snapshot(Model):
    version: Literal["r6-observe-v1"] = VERSION
    mode: Literal["observe"] = "observe"
    sampled_at: datetime
    items: list[Observation]
    truncated: bool = False


class RecoveryInput(Model):
    run_id: UUID
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
        if item.unknown_action_id:
            kind = "outcome_unknown"
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
                            "marker": str(item.unknown_action_id)
                            if item.unknown_action_id
                            else item.progress_cursor,
                        }
                    ),
                )
            )
    return result
