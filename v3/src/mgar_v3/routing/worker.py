from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from mgar_v3.domain import DomainError
from mgar_v3.routing.domain import Proposal, Proposals, RouterDecision
from mgar_v3.routing.store import CONTROL, addressed, question_for

if TYPE_CHECKING:
    from mgar_v3.coordination.engine import Engine, Row
    from mgar_v3.live import LivePermit


class RouterProvider(Protocol):
    mode: str

    def propose(self, batch: "Row") -> Proposals: ...


class FixtureRouter:
    mode = "fixture"

    def propose(self, batch: "Row") -> Proposals:
        proposals = []
        for group in batch["snapshot"]["threads"]:
            body = question_for(group["events"])
            target = None
            action = "ignore"
            message = None
            related = []
            for candidate in group["candidates"]:
                for kind, verb in CONTROL.items():
                    if body == f"/mgar {verb}-ticket {candidate['ticket_id']}":
                        action = kind
                        target = candidate["ticket_id"]
            if target is None and any(addressed(e["payload"]["body"]) for e in group["events"]):
                if (
                    body.strip() in {"help", "?", "帮忙", "帮我处理一下", "请帮忙"}
                    or len(body) > 8000
                ):
                    action = "request_clarification"
                    message = "clarification"
                else:
                    action = "create_ticket"
                    related = [
                        c["ticket_id"] for c in group["candidates"] if c["ticket_id"] in body
                    ][:3]
            proposals.append(
                Proposal(
                    thread_id=group["thread_id"],
                    action=action,
                    target_id=target,
                    related_ticket_ids=related,
                    message_code=message,
                )
            )
        return Proposals(proposals=proposals)


class RouterWorker:
    def __init__(
        self, engine: "Engine", provider: RouterProvider, permit: "LivePermit | None" = None
    ):
        self.engine, self.provider, self.permit = engine, provider, permit

    def tick(self) -> UUID | None:
        batch = self.engine.routing.prepare(self.provider.mode)
        if batch is None:
            return None
        try:
            if batch["status"] == "ready":
                decisions = [RouterDecision.model_validate(d) for d in batch["decisions"]]
            else:
                proposals = self.provider.propose(batch)
                decisions = self.engine.routing.wrap(batch, proposals)
                self.engine.routing.save(batch["batch_id"], decisions)
            self.engine.routing.commit(batch["batch_id"], decisions, permit=self.permit)
        except DomainError as exc:
            self.engine.routing.failure(batch["batch_id"], exc.code)
        except Exception:
            self.engine.routing.failure(batch["batch_id"], "ROUTER_FAILED")
        return UUID(str(batch["batch_id"]))
