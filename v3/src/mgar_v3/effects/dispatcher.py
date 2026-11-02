from typing import Protocol
from uuid import UUID

import httpx

from mgar_v3.coordination.engine import Engine, Row
from mgar_v3.domain import DomainError


class DefinitiveFailure(Exception):
    """The remote service definitively rejected the action without creating an object."""


class Transport(Protocol):
    mode: str

    def send(self, action: Row) -> str: ...

    def find(self, action: Row) -> str | None: ...


class Dispatcher:
    def __init__(self, engine: Engine, transport: Transport):
        self.engine = engine
        self.transport = transport

    def tick(self) -> UUID | None:
        action = self.engine.claim_effect(
            self.transport.mode,
            getattr(self.transport, "action_types", ("issue_comment",)),
            getattr(self.transport, "profile_hash", None),
        )
        if action is None:
            return None
        action_id = UUID(str(action["action_id"]))
        lease = UUID(str(action["lease_token"]))
        try:
            external_id = self.transport.send(action)
        except DefinitiveFailure:
            self.engine.effect_result(action_id, lease, failed=True)
        except (httpx.HTTPError, OSError, TimeoutError):
            self.engine.effect_result(action_id, lease, unknown=True)
        except Exception:
            self.engine.effect_result(action_id, lease, unknown=True)
            raise
        else:
            self.engine.effect_result(action_id, lease, external_id=external_id)
        return action_id

    def reconcile(self, action_id: UUID) -> bool:
        with self.engine.transaction() as conn:
            action, run = self.engine._action(conn, action_id)
            if run["execution_mode"] != self.transport.mode:
                raise DomainError(
                    "EXECUTION_MODE_MISMATCH", "Reconciliation transport mode differs"
                )
            if action["action_type"] not in getattr(
                self.transport, "action_types", ("issue_comment",)
            ):
                raise DomainError(
                    "TRANSPORT_TYPE_MISMATCH", "Transport cannot reconcile this action"
                )
        # No business transaction remains open during a remote lookup.
        external_id = self.transport.find(action)
        return self.engine.reconcile_effect(action_id, external_id)
