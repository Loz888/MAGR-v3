import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn

from mgar_v3.api import create_app
from mgar_v3.coordination.engine import Engine


@contextmanager
def mcp_authority(engine: Engine) -> Iterator[str]:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    url = "http://127.0.0.1:" + str(sock.getsockname()[1])
    engine.settings.r4_tool_base_url = url
    server = uvicorn.Server(
        uvicorn.Config(create_app(engine.settings, engine, run_background=False), log_level="error")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        raise RuntimeError("Local MCP authority failed to start")
    try:
        yield url
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        if thread.is_alive():
            raise RuntimeError("Local MCP authority did not stop")
