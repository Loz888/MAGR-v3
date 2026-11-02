from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

from mgar_v3.coordination.engine import Engine, Row
from mgar_v3.domain import DomainError


class ReadJobs:
    """Durable read-only work; the graph waits without blocking the control loop."""

    def __init__(self, engine: Engine):
        self.engine = engine

    def execute(self, run_id: UUID, epoch: int, read: Callable[[], tuple[bytes, str, str]]) -> Row:
        job = self.engine.submit_read_job(run_id, epoch, "read-source:v1")
        if job["status"] == "succeeded":
            return job
        if job["status"] != "queued":
            raise DomainError("READ_JOB_UNAVAILABLE", "Read job needs recovery or review")
        claim = self.engine.claim_read_job(job["job_id"], epoch)

        def work() -> Row:
            try:
                content, uri, version = read()
                return self.engine.finish_read_job(
                    claim["job_id"], claim["lease_token"], content, uri, version
                )
            except BaseException:
                self.engine.fail_read_job(claim["job_id"], claim["lease_token"])
                raise

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="mgar-read") as pool:
            return pool.submit(work).result()
