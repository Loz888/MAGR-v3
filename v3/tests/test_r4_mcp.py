import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn
from test_r4_experts import prepare

from mgar_v3.api import create_app
from mgar_v3.domain import DomainError
from mgar_v3.graph.runner import Runner
from mgar_v3.mcp.r4 import TaskTools


@contextmanager
def http_server(settings, engine):
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings, engine, run_background=False), log_level="error")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    try:
        yield "http://127.0.0.1:" + str(port)
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
        assert not thread.is_alive()


def test_actual_http_mcp_knowledge_data_and_all_experts(engine, settings, trigger):
    receipt = prepare(engine, trigger)
    with http_server(settings, engine) as url:
        engine.settings.r4_tool_base_url = url
        assert Runner(engine).execute(receipt.run_id)
        resolution = engine.tools.resolution(receipt.run_id)
        assert resolution["payload"]["complete"] and resolution["verdict"]["passed"]
        assert engine.tools.reader("owner").describe()["fixture_hash"]


def test_mcp_separate_endpoint_cannot_escalate_role(engine, settings, trigger):
    receipt = prepare(engine, trigger)
    run = engine.begin_run(receipt.run_id)
    token = engine.tools.issue(
        receipt.run_id, run["execution_epoch"], "knowledge", "knowledge", run["input_revision"]
    )
    with http_server(settings, engine) as url:
        engine.settings.r4_tool_base_url = url
        client = TaskTools(engine, token, "data")
        # SDK wraps server errors in an ExceptionGroup at the transport boundary.
        with pytest.raises((DomainError, ExceptionGroup)):
            client.call("describe_tables", {})
        with pytest.raises((DomainError, ExceptionGroup)):
            TaskTools(engine, "bad-token", "knowledge").call("recall_memory", {})
