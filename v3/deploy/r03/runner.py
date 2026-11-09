"""Trusted, stdlib-only Python test runner used inside the R03 sandbox.

It discovers ``test_*.py`` files, loads only ``unittest`` test cases and plain
``test_*`` functions, and emits one JSON line per test plus a summary.  Import
or collection failures are reported separately so the host can tell a build
failure from a test failure.  Nothing from the model is interpreted as code
here; the file inventory arrives through the trusted bootstrap.
"""

from __future__ import annotations

import importlib.util
import inspect
import io
import json
import os
import re
import sys
import unittest
from pathlib import Path
from types import ModuleType
from typing import Any

ROOT = Path("/workspace")
SKIP_DIRS = {"__pycache__", "node_modules", "vendor"}
MAX_FILES = 64


def emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def test_files() -> list[Path]:
    found: list[Path] = []
    for base, directories, names in os.walk(ROOT):
        directories[:] = sorted(
            name for name in directories if not name.startswith(".") and name not in SKIP_DIRS
        )
        for name in sorted(names):
            if name.startswith("test_") and name.endswith(".py"):
                found.append(Path(base) / name)
    return found[:MAX_FILES]


def module_name(label: str) -> str:
    return "mgar_sandbox_" + re.sub(r"[^A-Za-z0-9_]+", "_", label)


def load(path: Path, label: str) -> ModuleType:
    name = module_name(label)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {label}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def accepts_no_arguments(value: Any) -> bool:
    try:
        parameters = inspect.signature(value).parameters
    except (TypeError, ValueError):
        return False
    required = {
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    }
    return not any(
        parameter.default is inspect.Parameter.empty and parameter.kind in required
        for parameter in parameters.values()
    )


def build_suite(module: ModuleType, label: str) -> unittest.TestSuite:
    suite = unittest.TestSuite()
    suite.addTests(unittest.TestLoader().loadTestsFromModule(module))
    for name, value in sorted(vars(module).items()):
        if not name.startswith("test") or not callable(value) or isinstance(value, type):
            continue
        if not accepts_no_arguments(value):
            continue
        suite.addTest(FunctionTest(value, f"{label}::{name}"))
    return suite


class FunctionTest(unittest.TestCase):
    """Wrap a plain ``test_*`` function with a stable, repository-relative id."""

    def __init__(self, function: Any, label: str) -> None:
        super().__init__("runTest")
        self._function = function
        self._label = label

    def id(self) -> str:
        return self._label

    def __str__(self) -> str:
        return self._label

    def runTest(self) -> None:
        self._function()


class JsonResult(unittest.TextTestResult):
    def addSuccess(self, test: unittest.TestCase) -> None:
        super().addSuccess(test)
        emit({"event": "test", "name": test.id(), "status": "pass"})

    def addFailure(self, test: unittest.TestCase, err: Any) -> None:
        super().addFailure(test, err)
        emit(
            {
                "event": "test",
                "name": test.id(),
                "status": "fail",
                "message": self._exc_info_to_string(err, test)[-2000:],
            }
        )

    def addError(self, test: unittest.TestCase, err: Any) -> None:
        super().addError(test, err)
        emit(
            {
                "event": "test",
                "name": test.id(),
                "status": "error",
                "message": self._exc_info_to_string(err, test)[-2000:],
            }
        )

    def addSkip(self, test: unittest.TestCase, reason: str) -> None:
        super().addSkip(test, reason)
        emit({"event": "test", "name": test.id(), "status": "skip", "message": reason})


def main() -> int:
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(ROOT))
    suite = unittest.TestSuite()
    collection_errors = 0
    for path in test_files():
        label = path.relative_to(ROOT).as_posix()
        try:
            suite.addTests(build_suite(load(path, label), label))
        except BaseException as exc:  # noqa: BLE001 - one broken file must not hide the rest
            collection_errors += 1
            emit(
                {
                    "event": "collection_error",
                    "file": label,
                    "message": f"{type(exc).__name__}: {exc}"[-2000:],
                }
            )
    stream = io.StringIO()
    runner = unittest.TextTestRunner(stream=stream, verbosity=0, resultclass=JsonResult)
    result = runner.run(suite)
    failed = len(result.failures) + len(result.unexpectedSuccesses)
    errors = len(result.errors)
    skipped = len(result.skipped) + len(result.expectedFailures)
    emit(
        {
            "event": "summary",
            "tests": result.testsRun,
            "passed": result.testsRun - failed - errors - skipped,
            "failed": failed,
            "errors": errors,
            "skipped": skipped,
            "collection_errors": collection_errors,
        }
    )
    if collection_errors or failed or errors:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
