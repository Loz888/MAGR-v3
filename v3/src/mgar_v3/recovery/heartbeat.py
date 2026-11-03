import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from uuid import UUID

import psycopg

from mgar_v3.domain import DomainError
from mgar_v3.recovery.store import Recovery


@contextmanager
def worker_heartbeat(
    recovery: Recovery, run_id: UUID, epoch: Callable[[], int | None]
) -> Iterator[None]:
    stopped = threading.Event()

    def beat() -> None:
        while not stopped.wait(5):
            current = epoch()
            if current is not None:
                try:
                    recovery.heartbeat(run_id, current)
                except (DomainError, psycopg.Error):
                    return

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=6)
