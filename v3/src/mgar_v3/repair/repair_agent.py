"""Iterative repair loop with independent verification (V3.4 R04).

R04 closes the loop above R02 and R03.  A repair starts only when the public
tests actually fail at the handed-off base tree.  A pluggable provider then
returns a bounded, structured set of file edits; the edits pass through the
authorized R02 worktree, run against the public tests, and only then run
against a verification suite the provider never sees.  A candidate becomes a
Repair Proposal only after both stages pass.

Every rejected candidate produces structured feedback and consumes one attempt
inside an attempt, test-run and wall-clock budget.  Each attempt failure and
the final outcome are written as Episodic Memory episodes, so an exhausted
repair leaves its reason behind instead of silently claiming success.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import Annotated, Literal, Protocol, Self, cast

from pydantic import Field, model_validator

from mgar_v3.domain import DomainError, Model, digest
from mgar_v3.repair.sandbox_executor import (
    GoAdapter,
    Language,
    PythonAdapter,
    SandboxExecutor,
    TestAdapter,
    TestReceipt,
)
from mgar_v3.repair.worktree import (
    ChangedFile,
    FileEdit,
    PatchLimits,
    PatchResult,
    PatchWorktree,
    sha256_text,
)
from mgar_v3.repository_analysis.workspace import RepositorySnapshot

RepairStage = Literal["baseline", "patch", "public_tests", "verification"]
RepairStatus = Literal[
    "succeeded",
    "baseline_not_reproduced",
    "waiting_for_dependencies",
    "sandbox_error",
    "exhausted",
    "budget_exhausted",
    "deadline_exceeded",
    "provider_error",
]
EpisodeStatus = Literal[
    "succeeded",
    "baseline_not_reproduced",
    "waiting_for_dependencies",
    "sandbox_error",
    "exhausted",
    "budget_exhausted",
    "deadline_exceeded",
    "patch_rejected",
    "public_tests_failed",
    "verification_failed",
    "provider_error",
]
FeedbackStage = Literal["patch", "public_tests", "verification"]


class RepairBudget(Model):
    """Hard limits on model attempts, sandbox runs and wall-clock time."""

    max_attempts: Annotated[int, Field(ge=1, le=10)] = 3
    max_test_runs: Annotated[int, Field(ge=1, le=50)] = 10
    deadline_seconds: Annotated[float, Field(gt=0, le=3600)] = 900


class PatchPlan(Model):
    """The only answer shape a repair provider may return.

    The model supplies a bounded plan plus explicit file edits with pre-image
    hashes.  It never supplies a shell command; the trusted R03 adapter owns
    the test command.
    """

    summary: Annotated[str, Field(min_length=1, max_length=2000)]
    edits: Annotated[list[FileEdit], Field(min_length=1, max_length=16)]


class RepairFeedback(Model):
    """One structured rejection handed to the next model attempt."""

    attempt: Annotated[int, Field(ge=1, le=10)]
    stage: FeedbackStage
    code: Annotated[str, Field(min_length=1, max_length=100)]
    message: Annotated[str, Field(min_length=1, max_length=1000)]
    failed_tests: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)


class PatchProposalRejected(DomainError):
    """A provider rejection that the repair loop can feed back to the next try.

    ``retryable=False`` means the call must not be repeated: a paid outcome is
    unknown, usage could not be accounted, or the model interface is unavailable.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = True,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(code, message, 422)
        self.retryable = retryable
        self.details = dict(details or {})


class RepairPrompt(Model):
    """The public view of a repair: no hidden verification content."""

    repository: Annotated[str, Field(min_length=3, max_length=200)]
    objective: Annotated[str, Field(min_length=1, max_length=4000)]
    language: Language
    attempt: Annotated[int, Field(ge=1, le=10)]
    max_attempts: Annotated[int, Field(ge=1, le=10)]
    files: dict[str, str]
    allowed_paths: Annotated[list[str], Field(min_length=1, max_length=200)]
    limits: PatchLimits = Field(default_factory=PatchLimits)
    feedback: Annotated[list[RepairFeedback], Field(max_length=10)] = Field(default_factory=list)


class RepairRequest(Model):
    """A reproducible defect plus the independent verification it must satisfy."""

    repository: Annotated[str, Field(min_length=3, max_length=200)]
    objective: Annotated[str, Field(min_length=1, max_length=4000)]
    language: Language
    files: dict[str, str]
    allowed_paths: Annotated[list[str], Field(min_length=1, max_length=200)]
    frozen_paths: Annotated[list[str], Field(max_length=200)] = Field(default_factory=list)
    verification_files: dict[str, str]
    verification_tests: Annotated[list[str], Field(min_length=1, max_length=64)]
    budget: RepairBudget = Field(default_factory=RepairBudget)
    patch_limits: PatchLimits = Field(default_factory=PatchLimits)
    handoff_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None
    base_commit: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")] | None = None
    source_tree: Annotated[str, Field(pattern=r"^[a-f0-9]{40}$")] | None = None

    @classmethod
    def from_handoff(
        cls,
        handoff: object,
        snapshot: RepositorySnapshot,
        *,
        verification_files: dict[str, str],
        verification_tests: Sequence[str],
        budget: RepairBudget | None = None,
        patch_limits: PatchLimits | None = None,
        frozen_paths: Sequence[str] = (),
        language: Language | None = None,
    ) -> Self:
        """Bind a repair to the exact handoff, commit and snapshot bytes.

        The runtime uses this entry point so a repair can never start from a
        different commit than the handoff, nor from a snapshot whose per-file
        SHA-256 values do not reproduce the handed-off content.
        """

        from mgar_v3.repair.handoff import RepairHandoff

        if not isinstance(handoff, RepairHandoff):
            raise DomainError("REPAIR_HANDOFF_REQUIRED", "A RepairHandoff is required", 400)
        if handoff.computed_id() != handoff.handoff_id:
            raise DomainError(
                "REPAIR_HANDOFF_CORRUPTED",
                "Handoff id does not match its content",
                409,
            )
        if handoff.repository != snapshot.repository:
            raise DomainError(
                "REPAIR_REPOSITORY_MISMATCH",
                "Snapshot repository does not match the handoff",
                409,
            )
        if handoff.base_commit != snapshot.commit:
            raise DomainError(
                "REPAIR_COMMIT_MISMATCH",
                "Snapshot commit does not match the handoff base commit",
                409,
            )
        if handoff.reproduction.commit != snapshot.commit:
            raise DomainError(
                "REPAIR_REPRODUCTION_MISMATCH",
                "Reproduction receipt was produced at a different commit",
                409,
            )
        for path, expected in snapshot.file_sha256.items():
            content = snapshot.files.get(path)
            if content is None or sha256_text(content) != expected:
                raise DomainError(
                    "REPAIR_SNAPSHOT_MISMATCH",
                    f"{path!r} no longer matches the snapshot hash",
                    502,
                )
        return cls(
            repository=handoff.repository,
            objective=handoff.objective,
            language=language or _infer_language(snapshot.files),
            files=dict(snapshot.files),
            allowed_paths=list(handoff.allowed_paths),
            frozen_paths=list(frozen_paths),
            verification_files=dict(verification_files),
            verification_tests=list(verification_tests),
            budget=budget or RepairBudget(),
            patch_limits=patch_limits or PatchLimits(),
            handoff_id=handoff.handoff_id,
            base_commit=handoff.base_commit,
            source_tree=snapshot.tree,
        )

    @model_validator(mode="after")
    def independent_verification(self) -> Self:
        if not self.files:
            raise ValueError("repair requires a non-empty base tree")
        if not self.verification_files:
            raise ValueError("independent verification needs hidden test files")
        overlap = sorted(set(self.files) & set(self.verification_files))
        if overlap:
            raise ValueError(f"verification files overlap the public tree: {overlap}")
        if any(not name.strip() for name in self.verification_tests):
            raise ValueError("verification test names must not be blank")
        binding = (self.handoff_id, self.base_commit, self.source_tree)
        if any(item is not None for item in binding) and not all(
            item is not None for item in binding
        ):
            raise ValueError("handoff binding needs handoff_id, base_commit and source_tree")
        return self


class RepairAttempt(Model):
    """What happened in one Patch -> Test -> Feedback round."""

    attempt: Annotated[int, Field(ge=1, le=10)]
    plan_summary: str
    accepted: bool
    patch_id: str | None
    tree_before: str
    tree_after: str | None
    changed_files: list[ChangedFile] = Field(default_factory=list)
    public_receipt: TestReceipt | None = None
    verification_receipt: TestReceipt | None = None
    feedback: RepairFeedback | None = None


class RepairProposal(Model):
    """A verified candidate, ready for R05 review and publication."""

    schema_version: Literal["v34-repair-proposal-v1"] = "v34-repair-proposal-v1"
    proposal_id: str
    repository: str
    objective: str
    language: Language
    plan_summary: str
    base_tree: str
    tested_tree: str
    diff: str
    changed_files: list[ChangedFile]
    public_receipt: TestReceipt
    verification_receipt: TestReceipt
    attempts_used: Annotated[int, Field(ge=1, le=10)]
    verified: Literal[True] = True


class RepairEpisode(Model):
    """One Episodic Memory record of a failed attempt or a final outcome."""

    schema_version: Literal["v34-repair-episode-v1"] = "v34-repair-episode-v1"
    episode_id: str
    repository: str
    objective: str
    language: Language
    status: EpisodeStatus
    attempt: Annotated[int, Field(ge=0, le=10)]
    tree: str
    reason: Annotated[str, Field(min_length=1, max_length=1000)]
    failed_tests: Annotated[list[str], Field(max_length=64)] = Field(default_factory=list)


class RepairReport(Model):
    schema_version: Literal["v34-repair-report-v1"] = "v34-repair-report-v1"
    status: RepairStatus
    stage: RepairStage
    reason: str
    repository: str
    objective: str
    language: Language
    base_tree: str
    baseline: TestReceipt | None
    attempts: list[RepairAttempt]
    proposal: RepairProposal | None
    test_runs: Annotated[int, Field(ge=0, le=50)]


class EpisodeSink(Protocol):
    def record(self, episode: RepairEpisode) -> None: ...


class MemoryEpisodeLog:
    """In-process Episodic Memory sink used by tests and local runs."""

    def __init__(self) -> None:
        self.episodes: list[RepairEpisode] = []

    def record(self, episode: RepairEpisode) -> None:
        self.episodes.append(episode)


class RepairProvider(Protocol):
    def propose(self, prompt: RepairPrompt) -> PatchPlan: ...


class RepairObserver(Protocol):
    """Receives each repair node boundary so a caller can checkpoint the run."""

    def baseline_started(self) -> None: ...

    def baseline(self, receipt: TestReceipt) -> None: ...

    def provider_started(self, attempt: int) -> None: ...

    def planned(self, attempt: int, plan: PatchPlan) -> None: ...

    def patched(self, attempt: int, patch: PatchResult) -> None: ...

    def receipt(self, stage: str, attempt: int, receipt: TestReceipt) -> None: ...

    def attempt(self, record: RepairAttempt) -> None: ...


class _BudgetExhausted(Exception):
    pass


class _DeadlineExceeded(Exception):
    pass


def _adapter(language: Language) -> TestAdapter:
    if language == "go":
        return GoAdapter()
    return PythonAdapter()


def _infer_language(files: Mapping[str, str]) -> Language:
    """Infer the trusted adapter from the snapshot, never from model text."""

    counts = {"go": 0, "python": 0}
    for path in files:
        if path.endswith(".go"):
            counts["go"] += 1
        elif path.endswith((".py", ".pyi")):
            counts["python"] += 1
    return "go" if counts["go"] > counts["python"] else "python"


def _last_stage(attempts: Sequence[RepairAttempt]) -> RepairStage:
    if attempts and attempts[-1].feedback is not None:
        return attempts[-1].feedback.stage
    return "patch"


def _attempt_record(
    *,
    attempt: int,
    plan_summary: str,
    accepted: bool,
    patch: PatchResult | None,
    tree_before: str,
    public: TestReceipt | None,
    verification: TestReceipt | None,
    feedback: RepairFeedback | None,
) -> RepairAttempt:
    return RepairAttempt(
        attempt=attempt,
        plan_summary=plan_summary,
        accepted=accepted,
        patch_id=patch.patch_id if patch else None,
        tree_before=patch.tree_before if patch else tree_before,
        tree_after=patch.tree_after if patch else None,
        changed_files=list(patch.changed_files) if patch else [],
        public_receipt=public,
        verification_receipt=verification,
        feedback=feedback,
    )


class RepairAgent:
    """Runs the bounded repair loop against the fixed R03 sandbox image."""

    def __init__(
        self,
        *,
        executor: SandboxExecutor | None = None,
        memory: EpisodeSink | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.executor = executor or SandboxExecutor()
        self.memory: EpisodeSink = memory or MemoryEpisodeLog()
        self.clock = clock

    def run(
        self,
        request: RepairRequest,
        provider: RepairProvider,
        *,
        observer: RepairObserver | None = None,
    ) -> RepairReport:
        adapter = _adapter(request.language)
        started = self.clock()
        test_runs = 0
        attempts: list[RepairAttempt] = []
        feedback: list[RepairFeedback] = []

        def record(item: RepairAttempt) -> None:
            attempts.append(item)
            if observer is not None:
                observer.attempt(item)

        def execute(files: Mapping[str, str]) -> TestReceipt:
            nonlocal test_runs
            if test_runs >= request.budget.max_test_runs:
                raise _BudgetExhausted
            if self.clock() - started >= request.budget.deadline_seconds:
                raise _DeadlineExceeded
            test_runs += 1
            return self.executor.run(files, adapter)[0]

        def stop(
            status: RepairStatus,
            stage: RepairStage,
            reason: str,
            baseline_receipt: TestReceipt | None,
        ) -> RepairReport:
            self._remember(
                request,
                cast(EpisodeStatus, status),
                len(attempts),
                worktree.tree_hash(),
                reason,
                [],
            )
            return RepairReport(
                status=status,
                stage=stage,
                reason=reason,
                repository=request.repository,
                objective=request.objective,
                language=request.language,
                base_tree=base_tree,
                baseline=baseline_receipt,
                attempts=list(attempts),
                proposal=None,
                test_runs=test_runs,
            )

        with PatchWorktree(
            request.files,
            allowed_paths=request.allowed_paths,
            limits=request.patch_limits,
            expected_tree=request.source_tree,
        ) as worktree:
            base_tree = worktree.baseline_hash()
            if observer is not None:
                observer.baseline_started()
            try:
                baseline = execute(worktree.snapshot())
            except _BudgetExhausted:
                return stop(
                    "budget_exhausted", "baseline", "Repair test-run budget exhausted", None
                )
            except _DeadlineExceeded:
                return stop("deadline_exceeded", "baseline", "Repair deadline exhausted", None)
            if observer is not None:
                observer.baseline(baseline)

            if baseline.status == "waiting_for_dependencies":
                return stop(
                    "waiting_for_dependencies",
                    "baseline",
                    baseline.error or "Dependencies require a trusted offline bundle",
                    baseline,
                )
            if baseline.status in {"timeout", "sandbox_error"}:
                return stop(
                    "sandbox_error",
                    "baseline",
                    baseline.error or f"Baseline sandbox ended with {baseline.status}",
                    baseline,
                )
            if baseline.status != "test_failed":
                return stop(
                    "baseline_not_reproduced",
                    "baseline",
                    f"Baseline status {baseline.status} did not reproduce a failing public test",
                    baseline,
                )

            for attempt in range(1, request.budget.max_attempts + 1):
                if self.clock() - started >= request.budget.deadline_seconds:
                    return stop(
                        "deadline_exceeded",
                        _last_stage(attempts),
                        "Repair deadline exhausted",
                        baseline,
                    )
                if test_runs >= request.budget.max_test_runs:
                    return stop(
                        "budget_exhausted",
                        _last_stage(attempts),
                        "Repair test-run budget exhausted",
                        baseline,
                    )
                tree_before = worktree.tree_hash()
                if observer is not None:
                    observer.provider_started(attempt)
                try:
                    plan = provider.propose(
                        RepairPrompt(
                            repository=request.repository,
                            objective=request.objective,
                            language=request.language,
                            attempt=attempt,
                            max_attempts=request.budget.max_attempts,
                            files=worktree.snapshot(),
                            allowed_paths=list(request.allowed_paths),
                            limits=request.patch_limits,
                            feedback=list(feedback),
                        )
                    )
                except PatchProposalRejected as exc:
                    reason = f"{exc.code}: {exc}"
                    item = RepairFeedback(
                        attempt=attempt,
                        stage="patch",
                        code=exc.code,
                        message=str(exc)[:1000],
                    )
                    feedback.append(item)
                    record(
                        _attempt_record(
                            attempt=attempt,
                            plan_summary=f"Provider rejected: {exc.code}",
                            accepted=False,
                            patch=None,
                            tree_before=tree_before,
                            public=None,
                            verification=None,
                            feedback=item,
                        )
                    )
                    self._remember(
                        request, "patch_rejected", attempt, tree_before, reason, []
                    )
                    if not exc.retryable:
                        return stop("provider_error", "patch", reason, baseline)
                    continue
                if observer is not None:
                    observer.planned(attempt, plan)
                try:
                    frozen = sorted(
                        edit.path for edit in plan.edits if edit.path in set(request.frozen_paths)
                    )
                    if frozen:
                        raise DomainError(
                            "REPAIR_FROZEN_PATH",
                            "Frozen paths cannot be patched: " + ", ".join(frozen),
                            403,
                        )
                    patch = worktree.apply_patch(plan.edits)
                except DomainError as exc:
                    reason = f"{exc.code}: {exc}"
                    item = RepairFeedback(
                        attempt=attempt,
                        stage="patch",
                        code=exc.code,
                        message=str(exc)[:1000],
                    )
                    feedback.append(item)
                    record(
                        _attempt_record(
                            attempt=attempt,
                            plan_summary=plan.summary,
                            accepted=False,
                            patch=None,
                            tree_before=tree_before,
                            public=None,
                            verification=None,
                            feedback=item,
                        )
                    )
                    self._remember(
                        request, "patch_rejected", attempt, tree_before, reason, []
                    )
                    continue
                if observer is not None:
                    observer.patched(attempt, patch)

                try:
                    public = execute(worktree.snapshot())
                except _BudgetExhausted:
                    worktree.revert_patch()
                    return stop(
                        "budget_exhausted",
                        "public_tests",
                        "Repair test-run budget exhausted",
                        baseline,
                    )
                except _DeadlineExceeded:
                    worktree.revert_patch()
                    return stop(
                        "deadline_exceeded", "public_tests", "Repair deadline exhausted", baseline
                    )
                if observer is not None:
                    observer.receipt("public_tests", attempt, public)

                if public.status == "waiting_for_dependencies":
                    worktree.revert_patch()
                    return stop(
                        "waiting_for_dependencies",
                        "public_tests",
                        public.error or "Dependencies require a trusted offline bundle",
                        baseline,
                    )
                if public.status in {"timeout", "sandbox_error"}:
                    worktree.revert_patch()
                    return stop(
                        "sandbox_error",
                        "public_tests",
                        public.error or f"Public sandbox ended with {public.status}",
                        baseline,
                    )
                if public.status != "passed":
                    code = "PUBLIC_" + public.status.upper()
                    reason = public.error or f"Public tests ended with {public.status}"
                    item = RepairFeedback(
                        attempt=attempt,
                        stage="public_tests",
                        code=code,
                        message=reason[:1000],
                        failed_tests=list(public.failed_tests),
                    )
                    feedback.append(item)
                    record(
                        _attempt_record(
                            attempt=attempt,
                            plan_summary=plan.summary,
                            accepted=False,
                            patch=patch,
                            tree_before=tree_before,
                            public=public,
                            verification=None,
                            feedback=item,
                        )
                    )
                    self._remember(
                        request,
                        "public_tests_failed",
                        attempt,
                        patch.tree_after,
                        reason,
                        public.failed_tests,
                    )
                    worktree.revert_patch()
                    continue

                hidden = {**worktree.snapshot(), **request.verification_files}
                try:
                    verification = execute(hidden)
                except _BudgetExhausted:
                    worktree.revert_patch()
                    return stop(
                        "budget_exhausted",
                        "verification",
                        "Repair test-run budget exhausted",
                        baseline,
                    )
                except _DeadlineExceeded:
                    worktree.revert_patch()
                    return stop(
                        "deadline_exceeded", "verification", "Repair deadline exhausted", baseline
                    )
                if observer is not None:
                    observer.receipt("verification", attempt, verification)

                if verification.status == "waiting_for_dependencies":
                    worktree.revert_patch()
                    return stop(
                        "waiting_for_dependencies",
                        "verification",
                        verification.error or "Dependencies require a trusted offline bundle",
                        baseline,
                    )
                if verification.status in {"timeout", "sandbox_error"}:
                    worktree.revert_patch()
                    return stop(
                        "sandbox_error",
                        "verification",
                        verification.error
                        or f"Verification sandbox ended with {verification.status}",
                        baseline,
                    )
                if not self._verified(request, public, verification):
                    item = RepairFeedback(
                        attempt=attempt,
                        stage="verification",
                        code="VERIFICATION_FAILED",
                        message="Independent verification did not pass.",
                    )
                    feedback.append(item)
                    record(
                        _attempt_record(
                            attempt=attempt,
                            plan_summary=plan.summary,
                            accepted=False,
                            patch=patch,
                            tree_before=tree_before,
                            public=public,
                            verification=verification,
                            feedback=item,
                        )
                    )
                    self._remember(
                        request,
                        "verification_failed",
                        attempt,
                        patch.tree_after,
                        "Independent verification failed",
                        [],
                    )
                    worktree.revert_patch()
                    continue

                proposal = self._proposal(
                    request, plan, worktree, base_tree, public, verification, attempt
                )
                record(
                    _attempt_record(
                        attempt=attempt,
                        plan_summary=plan.summary,
                        accepted=True,
                        patch=patch,
                        tree_before=tree_before,
                        public=public,
                        verification=verification,
                        feedback=None,
                    )
                )
                self._remember(
                    request,
                    "succeeded",
                    attempt,
                    proposal.tested_tree,
                    f"Repair verified after {attempt} attempt(s)",
                    [],
                )
                return RepairReport(
                    status="succeeded",
                    stage="verification",
                    reason=f"Repair verified after {attempt} attempt(s)",
                    repository=request.repository,
                    objective=request.objective,
                    language=request.language,
                    base_tree=base_tree,
                    baseline=baseline,
                    attempts=list(attempts),
                    proposal=proposal,
                    test_runs=test_runs,
                )

            last = attempts[-1].feedback if attempts and attempts[-1].feedback else None
            reason = f"REPAIR_EXHAUSTED: {last.code}" if last else "REPAIR_EXHAUSTED"
            return stop("exhausted", _last_stage(attempts), reason, baseline)

    def _verified(
        self,
        request: RepairRequest,
        public: TestReceipt,
        verification: TestReceipt,
    ) -> bool:
        if verification.status != "passed":
            return False
        observed = set(verification.tests)
        return set(request.verification_tests) <= observed and set(public.tests) <= observed

    def _proposal(
        self,
        request: RepairRequest,
        plan: PatchPlan,
        worktree: PatchWorktree,
        base_tree: str,
        public: TestReceipt,
        verification: TestReceipt,
        attempt: int,
    ) -> RepairProposal:
        diff = worktree.get_diff()
        tested_tree = worktree.tree_hash()
        changed = worktree.get_changed_files()
        identity = digest(
            {
                "repository": request.repository,
                "base_tree": base_tree,
                "tested_tree": tested_tree,
                "diff": diff,
                "public": public.receipt_id,
                "verification": verification.receipt_id,
                "attempt": attempt,
            }
        )
        return RepairProposal(
            proposal_id=identity,
            repository=request.repository,
            objective=request.objective,
            language=request.language,
            plan_summary=plan.summary,
            base_tree=base_tree,
            tested_tree=tested_tree,
            diff=diff,
            changed_files=changed,
            public_receipt=public,
            verification_receipt=verification,
            attempts_used=attempt,
        )

    def _remember(
        self,
        request: RepairRequest,
        status: EpisodeStatus,
        attempt: int,
        tree: str,
        reason: str,
        failed_tests: Sequence[str],
    ) -> None:
        reason = reason[:1000]
        unique_failed = list(dict.fromkeys(failed_tests))[:64]
        episode_id = digest(
            {
                "repository": request.repository,
                "objective": request.objective,
                "language": request.language,
                "status": status,
                "attempt": attempt,
                "tree": tree,
                "reason": reason,
                "failed_tests": unique_failed,
            }
        )
        self.memory.record(
            RepairEpisode(
                episode_id=episode_id,
                repository=request.repository,
                objective=request.objective,
                language=request.language,
                status=status,
                attempt=attempt,
                tree=tree,
                reason=reason,
                failed_tests=unique_failed,
            )
        )
