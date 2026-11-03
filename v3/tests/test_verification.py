import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("verification_runner", ROOT / "scripts/verify.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)


def test_unknown_and_missing_targets_are_not_passed(tmp_path):
    assert runner.execute(tmp_path, "unknown", {})["status"] == "NOT_RUN"
    registry = {"missing": {"tests": ["missing.py"]}, "empty": {"tests": []}}
    assert runner.execute(tmp_path, "missing", registry)["status"] == "NOT_RUN"
    assert runner.execute(tmp_path, "empty", registry)["exit_code"] != 0


@pytest.mark.parametrize(
    "source,expected",
    [
        ("def test_success(): assert True\n", "PASS"),
        ("def test_failure(): assert False\n", "FAIL"),
        ("value = 1\n", "FAIL"),
        ("import pytest\n@pytest.mark.skip\ndef test_skip(): pass\n", "FAIL"),
    ],
)
def test_real_exit_status_and_junit_counts(tmp_path, source, expected):
    target = tmp_path / "test_probe.py"
    target.write_text(source)
    result = runner.execute(tmp_path, "probe", {"probe": {"tests": [target.name]}})
    assert result["status"] == expected
    assert (result["exit_code"] == 0) == (expected == "PASS")
    assert (tmp_path / result["log_path"]).is_file()


def test_timeout_preserves_nonzero_status(tmp_path):
    (tmp_path / "test_probe.py").write_text("import time\ndef test_timeout(): time.sleep(3)\n")
    result = runner.execute(
        tmp_path, "probe", {"probe": {"tests": ["test_probe.py"], "timeout_seconds": 0.1}}
    )
    assert result["command_exit_code"] == 124
    assert result["status"] == "FAIL"
    assert result["reason"] == "timeout"


def test_source_change_during_run_invalidates_otherwise_passing_receipt(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/runtime.py").write_text("version = 1\n")
    (tmp_path / "test_probe.py").write_text(
        "from pathlib import Path\n"
        "def test_changes_source():\n"
        "    Path('src/runtime.py').write_text('version = 2\\n')\n"
    )
    result = runner.execute(tmp_path, "probe", {"probe": {"tests": ["test_probe.py"]}})
    assert result["command_exit_code"] == 0
    assert result["status"] == "FAIL"
    assert result["reason"] == "source_changed_during_verification"
