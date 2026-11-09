"""V3.4 R03 general sandbox test executor tests.

The executor must run Go and Python tests inside a fixed, offline container,
never forward host files or credentials, deny network access, enforce CPU,
memory, PID and disk limits, keep a separate dependency-preparation phase, and
distinguish build failures, test failures, timeouts and sandbox errors.  These
tests use the real pinned image produced by ``make sandbox-build``.
"""

from __future__ import annotations

import hashlib

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.repair.sandbox_executor import (
    GO_COMMAND,
    PYTHON_COMMAND,
    GoAdapter,
    PythonAdapter,
    SandboxExecutor,
)
from mgar_v3.repair.workspace import tree

GO_MOD = "module example.invalid/r03fixture\n\ngo 1.27\n"
GO_CALC = "package mathx\n\nfunc Add(a, b int) int {\n\treturn a + b\n}\n"
GO_TEST = (
    'package mathx\n\nimport "testing"\n\n'
    'func TestAdd(t *testing.T) {\n\tif Add(2, 2) != 4 {\n\t\tt.Fatalf("unexpected")\n\t}\n}\n'
)
GO_FILES = {
    "go.mod": GO_MOD,
    "internal/mathx/calc.go": GO_CALC,
    "internal/mathx/calc_test.go": GO_TEST,
}
GO_BROKEN = "package mathx\n\nfunc Add(a, b int) int {\n\treturn a +\n}\n"
GO_DEPENDENCY_MOD = (
    "module example.invalid/r03dep\n\ngo 1.27\n\nrequire github.com/example/lib v1.2.3\n"
)

PY_CALC = "def add(a, b):\n    return a + b\n"
PY_TEST = "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
PY_FILES = {"src/calc.py": PY_CALC, "tests/test_calc.py": PY_TEST}
PY_FAILING = "def test_wrong():\n    assert 1 + 1 == 3\n"
PY_BROKEN_IMPORT = (
    "from src.broken import broken\n\n\ndef test_unreachable():\n    assert broken\n"
)
PY_BROKEN_SOURCE = "def broken(:\n    pass\n"
PY_DEPENDENCY = "import requests\n\n\ndef test_fetch():\n    assert requests\n"
PY_NETWORK = (
    "import socket\n\n\n"
    "def test_network_is_unreachable():\n"
    "    try:\n"
    '        connection = socket.create_connection(("1.1.1.1", 80), timeout=1)\n'
    "    except OSError:\n"
    "        return\n"
    "    connection.close()\n"
    '    raise AssertionError("sandbox reached the network")\n'
)
PY_SLOW = "import time\n\n\ndef test_slow():\n    time.sleep(5)\n"
PY_CRASH = (
    "import os\nimport signal\n\n\ndef test_crash():\n"
    "    os.kill(os.getpid(), signal.SIGKILL)\n"
)


def _executor(**kwargs: object) -> SandboxExecutor:
    return SandboxExecutor(**kwargs)  # type: ignore[arg-type]


def _python(files: dict[str, str], **kwargs: object) -> tuple[object, bytes]:
    return _executor().run(files, PythonAdapter(), **kwargs)  # type: ignore[arg-type]


def _go(files: dict[str, str], **kwargs: object) -> tuple[object, bytes]:
    return _executor().run(files, GoAdapter(), **kwargs)  # type: ignore[arg-type]


def test_python_tests_run_in_a_verified_isolated_sandbox() -> None:
    executor = SandboxExecutor()
    receipt, raw = executor.run(PY_FILES, PythonAdapter())

    assert receipt.status == "passed"
    assert receipt.tests == ["tests/test_calc.py::test_add"]
    assert receipt.passed_tests == 1
    assert receipt.failed_tests == []
    assert receipt.exit_code == 0
    assert receipt.command == PYTHON_COMMAND
    assert receipt.language == "python"
    assert receipt.adapter == "python-unittest-json-v1"
    assert receipt.image_id == executor.lock()["image_id"]
    assert receipt.preparation is not None
    assert receipt.preparation.status == "ready"
    assert receipt.preparation.toolchain.startswith("Python 3.11")
    assert receipt.log_sha256 == hashlib.sha256(raw).hexdigest()
    assert receipt.log_bytes == len(raw)
    assert len(raw) > 0
    assert receipt.workspace_tree == tree(dict(PY_FILES))
    assert receipt.duration_seconds > 0

    policy = receipt.policy
    assert policy is not None
    assert policy.network == "none"
    assert policy.read_only is True
    assert policy.privileged is False
    assert policy.mounts == 0
    assert policy.user == "65532:65532"
    assert policy.cap_drop == ["ALL"]
    assert policy.no_new_privileges is True
    assert policy.ipc == "none"
    assert policy.tmpfs == ["/tmp", "/workspace"]
    assert "PYTHONPATH" in policy.environment_keys
    assert not any("SECRET" in key or "TOKEN" in key for key in policy.environment_keys)


def test_go_tests_run_in_the_same_generic_sandbox() -> None:
    executor = SandboxExecutor()
    receipt, raw = executor.run(GO_FILES, GoAdapter())

    assert receipt.status == "passed"
    assert receipt.language == "go"
    assert receipt.adapter == "go-test-json-v1"
    assert "TestAdd" in receipt.tests
    assert receipt.failed_tests == []
    assert receipt.exit_code == 0
    assert receipt.command == GO_COMMAND
    assert receipt.image_id == executor.lock()["image_id"]
    assert receipt.preparation is not None
    assert receipt.preparation.status == "ready"
    assert receipt.preparation.toolchain.startswith("go version go1.27")
    assert receipt.log_sha256 == hashlib.sha256(raw).hexdigest()
    assert receipt.policy is not None
    assert receipt.policy.network == "none"


def test_network_is_denied_inside_the_sandbox() -> None:
    receipt, _raw = _python({"tests/test_network.py": PY_NETWORK})
    assert receipt.status == "passed"
    assert receipt.policy is not None
    assert receipt.policy.network == "none"


def test_host_files_and_credentials_are_not_visible(tmp_path, monkeypatch) -> None:
    sentinel = tmp_path / "host-sentinel.txt"
    sentinel.write_text("HOST ONLY CONTENT\n", encoding="utf-8")
    for name in ("GITHUB_TOKEN", "DEEPSEEK_API_KEY", "DATABASE_URL", "PGPASSWORD"):
        monkeypatch.setenv(name, "host-secret-" + name.lower())
    probe = (
        "import os\n\n\n"
        "SECRETS = (\n"
        '    "GITHUB_TOKEN",\n'
        '    "GITHUB_WRITE_TOKEN",\n'
        '    "DEEPSEEK_API_KEY",\n'
        '    "OPENAI_API_KEY",\n'
        '    "DATABASE_URL",\n'
        '    "PGPASSWORD",\n'
        ")\n\n\n"
        "def test_host_file_is_invisible():\n"
        f"    assert not os.path.exists({str(sentinel)!r})\n\n\n"
        "def test_host_credentials_are_absent():\n"
        "    assert not any(name in os.environ for name in SECRETS)\n"
    )
    receipt, _raw = _python({"tests/test_host.py": probe})

    assert receipt.status == "passed"
    assert receipt.policy is not None
    assert receipt.policy.mounts == 0
    assert "GITHUB_TOKEN" not in receipt.policy.environment_keys
    assert "DEEPSEEK_API_KEY" not in receipt.policy.environment_keys
    assert "DATABASE_URL" not in receipt.policy.environment_keys
    assert sentinel.read_text(encoding="utf-8") == "HOST ONLY CONTENT\n"


def test_build_failures_are_distinguished_from_test_failures() -> None:
    broken_python = {
        "src/broken.py": PY_BROKEN_SOURCE,
        "tests/test_broken.py": PY_BROKEN_IMPORT,
    }
    build_receipt, _raw = _python(broken_python)
    assert build_receipt.status == "build_failed"
    assert build_receipt.error == "Test collection failed"

    failing_receipt, _raw = _python({"tests/test_fail.py": PY_FAILING})
    assert failing_receipt.status == "test_failed"
    assert failing_receipt.failed_tests == ["tests/test_fail.py::test_wrong"]

    go_receipt, _raw = _go({**GO_FILES, "internal/mathx/calc.go": GO_BROKEN})
    assert go_receipt.status == "build_failed"
    assert go_receipt.exit_code != 0


def test_timeout_is_reported_and_the_container_is_killed() -> None:
    receipt, _raw = _python({"tests/test_slow.py": PY_SLOW}, timeout=1.5)
    assert receipt.status == "timeout"
    assert receipt.error is not None
    assert "time budget" in receipt.error
    assert receipt.duration_seconds < 5
    assert receipt.tests == []
    assert receipt.policy is not None


def test_sandbox_process_crash_is_reported_as_a_sandbox_error() -> None:
    receipt, _raw = _python({"tests/test_crash.py": PY_CRASH})
    assert receipt.status == "sandbox_error"
    assert receipt.error is not None
    assert "summary" in receipt.error


def test_unavailable_docker_is_a_sandbox_error_receipt() -> None:
    executor = SandboxExecutor(docker="/nonexistent/mgar-v3-docker")
    receipt, raw = executor.run(PY_FILES, PythonAdapter())
    assert receipt.status == "sandbox_error"
    assert receipt.preparation is None
    assert receipt.policy is None
    assert receipt.error is not None
    assert "unavailable" in receipt.error
    assert raw == b""


def test_dependency_preparation_waits_instead_of_installing() -> None:
    python_receipt, _raw = _python({"tests/test_fetch.py": PY_DEPENDENCY})
    assert python_receipt.status == "waiting_for_dependencies"
    assert python_receipt.preparation is not None
    assert python_receipt.preparation.status == "waiting"
    assert "requests" in python_receipt.preparation.requirements
    assert python_receipt.command == []
    assert python_receipt.exit_code is None
    assert python_receipt.policy is None

    go_receipt, _raw = _go(
        {
            "go.mod": GO_DEPENDENCY_MOD,
            "internal/mathx/calc.go": GO_CALC,
            "internal/mathx/calc_test.go": GO_TEST,
        }
    )
    assert go_receipt.status == "waiting_for_dependencies"
    assert go_receipt.preparation is not None
    assert "github.com/example/lib" in go_receipt.preparation.requirements
    assert go_receipt.command == []


def test_invalid_or_missing_sandbox_inputs_are_rejected() -> None:
    with pytest.raises(DomainError) as empty:
        SandboxExecutor().run({}, PythonAdapter())
    assert empty.value.code == "SANDBOX_SOURCE_MISSING"

    with pytest.raises(DomainError) as denied:
        SandboxExecutor().run({"../escape.py": "x = 1\n"}, PythonAdapter())
    assert denied.value.code == "SANDBOX_PATH_DENIED"
