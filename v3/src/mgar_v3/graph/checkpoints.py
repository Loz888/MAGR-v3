import threading
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row

_SETUP_LOCK = threading.Lock()


@contextmanager
def checkpoint_saver(dsn: str) -> Iterator[PostgresSaver]:
    with psycopg.connect(
        dsn,
        autocommit=True,
        row_factory=dict_row,
        prepare_threshold=0,
        options="-c search_path=checkpoints,public",
    ) as connection:
        saver = PostgresSaver(connection)
        # Official setup migrations are not designed to race on a cold schema.
        with _SETUP_LOCK:
            saver.setup()
        yield saver
