import json
import time
from pathlib import Path
from threading import Event as ThreadEvent
from typing import Any
from uuid import UUID

import httpx
import redis

from mgar_v3.benchmark.domain import EvaluationAuthorization
from mgar_v3.coordination_eval.domain import WORKER_SYSTEM
from mgar_v3.coordination_eval.store import DATA, Journal
from mgar_v3.domain import Investigation, TriggerInput, Verification, digest
from mgar_v3.graph.runner import FixtureInvestigator
from mgar_v3.live import Authorization, LivePermit, LiveProfile
from mgar_v3.models.structured import StructuredModel


class SourceCache:
    def __init__(self, journal: Journal):
        self.journal = journal
        self.client = redis.Redis.from_url(
            journal.engine.settings.redis_url.get_secret_value(), socket_timeout=3
        )
        self.source = (DATA / "source.md").read_bytes()
        self.key = "mgar:coord-eval:" + journal.id + ":source:" + digest(self.source.decode())

    def prepare(self, warm: bool) -> None:
        self.client.delete(self.key)  # Only this trial's dedicated key, never FLUSHDB.
        if warm:
            started = time.perf_counter()
            self.client.set(self.key, self.source, ex=3600)
            self.journal.audit("cache_prewarm", seconds=time.perf_counter() - started)

    def read(self) -> bytes:
        started = time.perf_counter()
        cached = self.client.get(self.key)
        hit = isinstance(cached, bytes) and cached == self.source
        if not hit:
            self.client.set(self.key, self.source, ex=3600)
        self.journal.audit("source_cache", hit=hit, seconds=time.perf_counter() - started)
        return self.source


class Slots:
    def __init__(
        self,
        journal: Journal,
        manifest: dict[str, Any],
        authorization: EvaluationAuthorization,
        client: httpx.Client | None = None,
    ):
        self.journal, self.manifest, self.auth, self.client = (
            journal,
            manifest,
            authorization,
            client,
        )
        self.hash = digest(manifest)

    def item(self, key: str) -> dict[str, Any]:
        return next(
            p
            for p in self.manifest["profiles"]
            if p["trial_id"] == self.journal.id and p["slot"] == key
        )

    def permit(self, key: str) -> LivePermit:
        profile = LiveProfile.model_validate(self.item(key)["profile"])
        return LivePermit(
            profile,
            Authorization(
                profile_hash=profile.profile_hash,
                approved_by=self.auth.approved_by,
                expires_at=self.auth.expires_at,
                allow_paid=self.auth.allow_paid,
                allow_external_write=False,
            ),
            self.journal.engine.settings,
        )

    def control_run(self, key: str) -> UUID:
        permit = self.permit(key)
        p, engine = permit.profile, self.journal.engine
        receipt = engine.ingest(
            TriggerInput(
                source="cli",
                external_event_id="coord-slot:" + p.case_id,
                project_id=p.project_id,
                repository=p.repository,
                issue_number=p.issue_number,
                actor=p.actor,
                body="/mgar investigate " + p.question,
                execution_mode="live",
            ),
            permit,
        )
        assert receipt.run_id is not None
        self.journal.readonly(receipt.run_id, permit, self.hash, self.item(key)["case_id"])
        return receipt.run_id

    def finish_control(self, run_id: UUID, epoch: int, payload: dict[str, Any]) -> None:
        self.journal.engine.commit_report(
            run_id,
            epoch,
            json.dumps(payload).encode(),
            Verification(passed=True, checks=["typed_router_plan_receipt"], reasons=[]),
        )


class FixedInvestigator(FixtureInvestigator):
    mode = "live"

    def __init__(
        self,
        slots: Slots,
        key: str,
        run_id: UUID,
        cache: SourceCache,
        entered: ThreadEvent,
        release: ThreadEvent,
    ):
        self.slots, self.key, self.run_id, self.cache = slots, key, run_id, cache
        self.entered, self.release = entered, release

    def read(self) -> tuple[bytes, str, str]:
        content = self.cache.read()
        self.entered.set()
        if not self.release.wait(600):
            raise TimeoutError("Frozen follow-up phase did not release source read")
        import hashlib

        return (
            content,
            "coordination-source://synthetic-policy",
            hashlib.sha256(content).hexdigest(),
        )

    def investigate(self, question: str, content: str, evidence_id: UUID) -> Investigation:
        self.slots.journal.audit(
            "worker_model_input",
            run_id=str(self.run_id),
            question=question,
            source_sha256=digest(content),
        )
        return StructuredModel(
            self.slots.journal.engine, self.slots.permit(self.key), self.slots.client
        ).complete(
            self.run_id,
            self.execution_epoch,
            self.model_step_key,
            WORKER_SYSTEM,
            {"question": question, "source": content, "evidence_id": str(evidence_id)},
            Investigation,
        )


def write_json(path: Path, value: Any) -> None:
    import os

    encoded = (json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n").encode()
    tmp = path.with_suffix(path.suffix + ".pending")
    with tmp.open("wb") as out:
        os.fchmod(out.fileno(), 0o600)
        out.write(encoded)
        out.flush()
        os.fsync(out.fileno())
    tmp.replace(path)
