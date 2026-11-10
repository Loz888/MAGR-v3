"""V3.5 R01 structured model patch provider tests.

The provider is the only bridge between a paid model reply and the trusted
worktree.  It must accept full-file edits with a matching pre-image hash, and
reject malformed JSON, truncation, missing usage, path escapes, stale hashes,
budget overruns, protected manifests, model diffs and shell text with a
machine-readable code the repair loop can feed back into the next attempt.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from mgar_v3.domain import DomainError, digest
from mgar_v3.repair.providers import (
    ModelPricing,
    StructuredPatchProvider,
)
from mgar_v3.repair.repair_agent import (
    MemoryEpisodeLog,
    PatchProposalRejected,
    RepairAgent,
    RepairPrompt,
    RepairRequest,
)
from mgar_v3.repair.sandbox_executor import TestReceipt as SandboxTestReceipt
from mgar_v3.repair.worktree import FileEdit, PatchLimits, PatchWorktree

CALC_BUG = "def add(a, b):\n    return a - b\n"
CALC_FIXED = "def add(a, b):\n    return a + b\n"
CALC_TEST = "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 2) == 4\n"
CALC_HIDDEN = (
    "from src.calc import add\n\n\ndef test_add_negative():\n    assert add(-3, -4) == -7\n"
)
FILES = {"src/calc.py": CALC_BUG, "tests/test_calc.py": CALC_TEST}

EXPERIMENT = Path(__file__).resolve().parents[1] / "experiments/v34-audit-repair"
E01_REPAIRS = [
    (
        "click-sentinel-repair",
        "snapshots/click/3cbcf9b11546f4cf10b36d3e2e531733ba6fe001",
        "snapshots/click/4295457ddc7b5ca9a733d59ca40d9651b02b9b1d",
        "src/click/_utils.py",
    ),
    (
        "click-short-help-repair",
        "snapshots/click/6d5bac5c712195cbfa9666ba0ecbf0573cdea356",
        "snapshots/click/f67c2bb6c2f055c1c7a57cd46c3697fc26a7742e",
        "src/click/utils.py",
    ),
    (
        "click-help-name-repair",
        "snapshots/click/8c1a0a7abbc1c36f70d1f65f3604acc46c5ce6ab",
        "snapshots/click/b67832c2167e5b0ff6764a8c04a0a9087e697b5a",
        "src/click/core.py",
    ),
]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class MockModel:
    """A scripted raw completion model with no network and no paid call."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []

    def complete(self, *, system: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls.append({"system": system, "payload": payload})
        if not self.replies:
            raise AssertionError("provider made an unexpected extra model call")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def _reply(content: str, **overrides: Any) -> dict[str, Any]:
    reply: dict[str, Any] = {
        "content": content,
        "usage": {"prompt_tokens": 120, "completion_tokens": 40},
        "model": "mock-repair",
        "latency_ms": 12.5,
        "finish_reason": "stop",
    }
    reply.update(overrides)
    return reply


def _plan_reply(plan: Mapping[str, Any], **overrides: Any) -> dict[str, Any]:
    return _reply(json.dumps(plan), **overrides)


def _update_plan(path: str, before: str, after: str, summary: str = "Apply the fix") -> dict:
    return {
        "summary": summary,
        "edits": [
            {
                "path": path,
                "operation": "update",
                "expected_sha256": _sha(before),
                "content": after,
            }
        ],
    }


def _prompt(
    files: Mapping[str, str] | None = None,
    allowed_paths: list[str] | None = None,
    *,
    limits: PatchLimits | None = None,
    feedback: list[Any] | None = None,
    attempt: int = 1,
) -> RepairPrompt:
    return RepairPrompt(
        repository="example/r01-fixture",
        objective="Fix the add function so the public test passes",
        language="python",
        attempt=attempt,
        max_attempts=3,
        files=dict(files if files is not None else FILES),
        allowed_paths=allowed_paths if allowed_paths is not None else ["src/calc.py"],
        limits=limits or PatchLimits(),
        feedback=feedback or [],
    )


def _reject(provider: StructuredPatchProvider, prompt: RepairPrompt, code: str) -> None:
    with pytest.raises(PatchProposalRejected) as raised:
        provider.propose(prompt)
    assert raised.value.code == code


def test_structured_update_is_validated_applied_and_accounted() -> None:
    model = MockModel(_plan_reply(_update_plan("src/calc.py", CALC_BUG, CALC_FIXED)))
    provider = StructuredPatchProvider(
        model,
        pricing=ModelPricing(
            input_micros_per_million=500_000, output_micros_per_million=1_000_000
        ),
    )
    plan = provider.propose(_prompt())
    assert isinstance(plan.edits[0], FileEdit)
    assert plan.edits[0].expected_sha256 == _sha(CALC_BUG)

    with PatchWorktree(FILES, allowed_paths=["src/calc.py"]) as worktree:
        result = worktree.apply_patch(plan.edits)
        assert worktree.snapshot()["src/calc.py"] == CALC_FIXED
        assert result.files_changed == 1
        diff = worktree.get_diff()
    assert "return a + b" in diff
    assert diff.startswith("diff --git a/src/calc.py b/src/calc.py")

    stats = provider.stats()
    assert stats.calls == 1 and stats.accepted == 1 and stats.rejected == 0
    assert stats.success_rate == 1.0
    assert stats.prompt_tokens == 120 and stats.completion_tokens == 40
    assert stats.cost_micros == 100
    recorded = json.dumps([call.model_dump() for call in provider.calls])
    assert CALC_BUG not in recorded and CALC_FIXED not in recorded


def test_prompt_is_minimal_and_never_contains_hidden_verification() -> None:
    model = MockModel(
        _plan_reply(_update_plan("src/calc.py", CALC_BUG, CALC_FIXED))
    )
    provider = StructuredPatchProvider(model)
    files = {**FILES, "verification/test_calc_hidden.py": CALC_HIDDEN}
    provider.propose(_prompt(files, ["src/calc.py", "tests/test_calc.py"]))
    payload = model.calls[0]["payload"]
    assert payload["allowed_paths"] == ["src/calc.py", "tests/test_calc.py"]
    assert set(payload["files"]) == {"src/calc.py", "tests/test_calc.py"}
    assert payload["files"]["src/calc.py"]["sha256"] == _sha(CALC_BUG)
    encoded = json.dumps(payload)
    assert CALC_HIDDEN not in encoded
    assert "verification/" not in encoded
    assert "hidden" not in encoded.lower()


@pytest.mark.parametrize(
    ("content", "code"),
    [
        ("not json at all", "PATCH_FORMAT"),
        ('{"summary": "x", "edits": []}', "PATCH_FORMAT"),
        ('```json\n{"summary": "x", "edits": []}\n```', "PATCH_FORMAT"),
    ],
)
def test_malformed_json_is_a_recoverable_structured_error(content: str, code: str) -> None:
    provider = StructuredPatchProvider(MockModel(_reply(content)))
    _reject(provider, _prompt(), code)
    assert provider.stats().parse_errors == 1
    assert provider.calls[0].retryable is True


def test_truncated_model_output_is_reported() -> None:
    provider = StructuredPatchProvider(
        MockModel(_reply("", finish_reason="length"))
    )
    _reject(provider, _prompt(), "PATCH_TRUNCATED")
    assert provider.stats().completion_tokens == 40


def test_missing_usage_is_rejected_and_never_resent() -> None:
    model = MockModel(
        {
            "content": json.dumps(_update_plan("src/calc.py", CALC_BUG, CALC_FIXED)),
            "model": "mock-repair",
        }
    )
    provider = StructuredPatchProvider(model)
    with pytest.raises(PatchProposalRejected) as raised:
        provider.propose(_prompt())
    assert raised.value.code == "PATCH_USAGE_MISSING"
    assert raised.value.retryable is False
    with pytest.raises(PatchProposalRejected) as repeated:
        provider.propose(_prompt())
    assert repeated.value.code == "PATCH_OUTCOME_UNKNOWN"
    assert len(model.calls) == 1
    assert provider.stats().unknown_outcomes == 1


@pytest.mark.parametrize(
    "path",
    ["../secret.py", "/etc/passwd", ".git/config", ".github/workflows/ci.yml"],
)
def test_path_escape_and_git_paths_are_rejected(path: str) -> None:
    plan = {
        "summary": "escape",
        "edits": [
            {"path": path, "operation": "create", "content": "print('x')\n"}
        ],
    }
    provider = StructuredPatchProvider(MockModel(_plan_reply(plan)))
    _reject(provider, _prompt(), "PATCH_PATH_DENIED")


def test_path_outside_the_authorized_set_is_rejected() -> None:
    plan = _update_plan("tests/test_calc.py", CALC_TEST, CALC_TEST + "\n# changed\n")
    provider = StructuredPatchProvider(MockModel(_plan_reply(plan)))
    _reject(provider, _prompt(), "PATCH_PATH_DENIED")


def test_stale_pre_image_hash_is_a_recoverable_error() -> None:
    plan = _update_plan("src/calc.py", "def add(a, b):\n    return 0\n", CALC_FIXED)
    provider = StructuredPatchProvider(MockModel(_plan_reply(plan)))
    with pytest.raises(PatchProposalRejected) as raised:
        provider.propose(_prompt())
    assert raised.value.code == "PATCH_STALE"
    assert raised.value.retryable is True
    assert provider.stats().accepted == 0


def test_patch_over_the_line_budget_is_rejected() -> None:
    large = "\n".join(f"line_{index} = {index}" for index in range(40)) + "\n"
    plan = _update_plan("src/calc.py", CALC_BUG, large)
    provider = StructuredPatchProvider(MockModel(_plan_reply(plan)))
    _reject(provider, _prompt(limits=PatchLimits(max_changed_lines=5)), "PATCH_LIMIT")


def test_dependency_manifest_is_protected_even_if_authorized() -> None:
    files = {**FILES, "requirements.txt": "click==8.1.0\n"}
    plan = _update_plan(
        "requirements.txt", "click==8.1.0\n", "click==8.1.0\nrequests==2.31.0\n"
    )
    provider = StructuredPatchProvider(MockModel(_plan_reply(plan)))
    _reject(
        provider,
        _prompt(files, ["requirements.txt", "src/calc.py"]),
        "PATCH_PROTECTED",
    )


def test_model_command_and_diff_fields_are_never_executed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("the provider must never execute model text")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)

    command_plan = {
        "summary": "run",
        "command": "rm -rf /",
        "edits": [],
    }
    provider = StructuredPatchProvider(MockModel(_plan_reply(command_plan)))
    _reject(provider, _prompt(), "PATCH_COMMAND_REJECTED")

    diff_plan = {
        "summary": "patch",
        "edits": [
            {
                "path": "src/calc.py",
                "operation": "update",
                "expected_sha256": _sha(CALC_BUG),
                "content": CALC_FIXED,
                "diff": "@@ -1,2 +1,2 @@",
            }
        ],
    }
    provider = StructuredPatchProvider(MockModel(_plan_reply(diff_plan)))
    _reject(provider, _prompt(), "PATCH_DIFF_UNSUPPORTED")


def test_create_and_update_existence_is_enforced() -> None:
    existing = {
        "summary": "conflict",
        "edits": [{"path": "src/calc.py", "operation": "create", "content": CALC_FIXED}],
    }
    provider = StructuredPatchProvider(MockModel(_plan_reply(existing)))
    _reject(provider, _prompt(), "PATCH_CONFLICT")

    missing = {
        "summary": "missing",
        "edits": [
            {
                "path": "src/missing.py",
                "operation": "update",
                "expected_sha256": _sha("anything"),
                "content": "x = 1\n",
            }
        ],
    }
    provider = StructuredPatchProvider(MockModel(_plan_reply(missing)))
    _reject(
        provider,
        _prompt(FILES, ["src/calc.py", "src/missing.py"]),
        "PATCH_NOT_FOUND",
    )


def test_unknown_transport_outcome_is_not_retried() -> None:
    model = MockModel(DomainError("USAGE_UNKNOWN", "outcome unknown"))
    provider = StructuredPatchProvider(model)
    with pytest.raises(PatchProposalRejected) as raised:
        provider.propose(_prompt())
    assert raised.value.code == "PATCH_OUTCOME_UNKNOWN"
    assert raised.value.retryable is False
    with pytest.raises(PatchProposalRejected) as repeated:
        provider.propose(_prompt())
    assert repeated.value.code == "PATCH_OUTCOME_UNKNOWN"
    assert len(model.calls) == 1


@pytest.mark.parametrize(("task_id", "buggy_dir", "fixed_dir", "path"), E01_REPAIRS)
def test_e01_patch_failures_replay_to_applied_or_recoverable(
    task_id: str, buggy_dir: str, fixed_dir: str, path: str
) -> None:
    buggy = (EXPERIMENT / buggy_dir / path).read_text(encoding="utf-8")
    fixed = (EXPERIMENT / fixed_dir / path).read_text(encoding="utf-8")
    assert buggy != fixed
    files = {path: buggy}
    allowed = [path]

    applied = StructuredPatchProvider(
        MockModel(_plan_reply(_update_plan(path, buggy, fixed)))
    )
    wide = PatchLimits(max_file_bytes=262_144, max_total_bytes=262_144)
    plan = applied.propose(_prompt(files, allowed, limits=wide))
    with PatchWorktree(files, allowed_paths=allowed, limits=wide) as worktree:
        worktree.apply_patch(plan.edits)
        assert worktree.snapshot()[path] == fixed

    stale_plan = _update_plan(path, "outdated snapshot\n", fixed)
    stale = StructuredPatchProvider(MockModel(_plan_reply(stale_plan)))
    with pytest.raises(PatchProposalRejected) as raised:
        stale.propose(_prompt(files, allowed))
    assert raised.value.code == "PATCH_STALE"
    assert raised.value.retryable is True
    assert stale.stats().accepted == 0


def _receipt(
    status: str,
    tests: list[str],
    *,
    failed: list[str] | None = None,
    files: Mapping[str, str] | None = None,
) -> SandboxTestReceipt:
    failed = failed or []
    return SandboxTestReceipt(
        receipt_id=digest({"status": status, "tests": tests, "failed": failed}),
        language="python",
        adapter="python-pytest",
        status=status,  # type: ignore[arg-type]
        preparation=None,
        image=None,
        image_id=None,
        command=["python", "-m", "pytest"],
        tests=tests,
        passed_tests=sum(name not in failed for name in tests),
        failed_tests=failed,
        skipped_tests=0,
        exit_code=0 if status == "passed" else 1,
        duration_seconds=0.1,
        log_sha256=_sha(status + "".join(tests)),
        log_bytes=1,
        log_truncated=False,
        workspace_tree=digest(dict(files or {})),
        policy=None,
        error=None,
    )


class FakeExecutor:
    """Returns a failing baseline, then public and hidden passes after the fix."""

    def __init__(self) -> None:
        self.calls = 0

    def run(self, files: Mapping[str, str], _adapter: object, **_kwargs: object):
        self.calls += 1
        if "verification/test_calc_hidden.py" in files:
            return (
                _receipt(
                    "passed",
                    [
                        "tests/test_calc.py::test_add",
                        "verification/test_calc_hidden.py::test_add_negative",
                    ],
                    files=files,
                ),
                b"",
            )
        if files.get("src/calc.py") == CALC_FIXED:
            return _receipt("passed", ["tests/test_calc.py::test_add"], files=files), b""
        return (
            _receipt(
                "test_failed",
                ["tests/test_calc.py::test_add"],
                failed=["tests/test_calc.py::test_add"],
                files=files,
            ),
            b"",
        )


def test_agent_feeds_a_structured_rejection_back_into_the_next_attempt() -> None:
    model = MockModel(
        _plan_reply(_update_plan("src/calc.py", "stale pre-image", CALC_FIXED)),
        _plan_reply(_update_plan("src/calc.py", CALC_BUG, CALC_FIXED)),
    )
    provider = StructuredPatchProvider(model)
    memory = MemoryEpisodeLog()
    executor = FakeExecutor()
    request = RepairRequest(
        repository="example/r01-fixture",
        objective="Fix the add function so the public and independent tests agree",
        language="python",
        files=dict(FILES),
        allowed_paths=["src/calc.py"],
        verification_files={"verification/test_calc_hidden.py": CALC_HIDDEN},
        verification_tests=["verification/test_calc_hidden.py::test_add_negative"],
    )
    report = RepairAgent(executor=executor, memory=memory).run(request, provider)  # type: ignore[arg-type]
    assert report.status == "succeeded"
    assert report.attempts[0].feedback is not None
    assert report.attempts[0].feedback.code == "PATCH_STALE"
    assert report.attempts[1].accepted is True
    assert executor.calls == 3
    assert model.calls[1]["payload"]["feedback"][0]["code"] == "PATCH_STALE"
    assert [episode.status for episode in memory.episodes] == [
        "patch_rejected",
        "succeeded",
    ]


def test_agent_stops_on_a_non_retryable_provider_error() -> None:
    model = MockModel({"content": "{}", "model": "mock-repair"})
    provider = StructuredPatchProvider(model)
    memory = MemoryEpisodeLog()
    executor = FakeExecutor()
    request = RepairRequest(
        repository="example/r01-fixture",
        objective="Fix the add function so the public and independent tests agree",
        language="python",
        files=dict(FILES),
        allowed_paths=["src/calc.py"],
        verification_files={"verification/test_calc_hidden.py": CALC_HIDDEN},
        verification_tests=["verification/test_calc_hidden.py::test_add_negative"],
    )
    report = RepairAgent(executor=executor, memory=memory).run(request, provider)  # type: ignore[arg-type]
    assert report.status == "provider_error"
    assert report.reason is not None and "PATCH_USAGE_MISSING" in report.reason
    assert len(model.calls) == 1
    assert len(report.attempts) == 1
    assert report.attempts[0].feedback is not None
    assert report.attempts[0].feedback.code == "PATCH_USAGE_MISSING"
    assert executor.calls == 1
    assert [episode.status for episode in memory.episodes] == [
        "patch_rejected",
        "provider_error",
    ]
