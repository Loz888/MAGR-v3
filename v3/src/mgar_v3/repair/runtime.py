"""Handoff-driven, sandbox-only repair runtime (V3.5 R02).

R02 closes the gap between a real handoff and real repository bytes: the
runtime rebuilds an isolated worktree from a byte-exact snapshot of the fixed
commit and executes every test through the restricted R03 container.  No
fixture file, host ``pytest`` process or model-supplied command is reachable
from this path.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from typing import TYPE_CHECKING

from mgar_v3.domain import DomainError
from mgar_v3.repair.repair_agent import (
    EpisodeSink,
    MemoryEpisodeLog,
    RepairAgent,
    RepairBudget,
    RepairProvider,
    RepairReport,
    RepairRequest,
)
from mgar_v3.repair.sandbox_executor import SandboxExecutor
from mgar_v3.repair.worktree import PatchLimits, PatchWorktree
from mgar_v3.repository_analysis.workspace import RepositorySnapshot

if TYPE_CHECKING:
    from pathlib import Path

    from mgar_v3.repair.handoff import RepairHandoff


class SnapshotRepairRuntime:
    """Run one repair strictly from a handoff plus its fixed-commit snapshot."""

    def __init__(
        self,
        *,
        executor: SandboxExecutor | None = None,
        memory: EpisodeSink | None = None,
        worktree_root: str | Path | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.executor = executor or SandboxExecutor()
        self.memory: EpisodeSink = memory or MemoryEpisodeLog()
        self.worktree_root = worktree_root
        self.clock = clock

    def prepare_worktree(
        self,
        handoff: RepairHandoff,
        snapshot: RepositorySnapshot,
        *,
        limits: PatchLimits | None = None,
    ) -> PatchWorktree:
        """Rebuild the handed-off commit as an isolated, disposable worktree."""

        return PatchWorktree.from_snapshot(
            handoff,
            snapshot,
            limits=limits,
            worktree_root=self.worktree_root,
        )

    def run(
        self,
        handoff: RepairHandoff,
        snapshot: RepositorySnapshot,
        provider: RepairProvider,
        *,
        verification_files: Mapping[str, str],
        verification_tests: Sequence[str],
        budget: RepairBudget | None = None,
        patch_limits: PatchLimits | None = None,
        frozen_paths: Sequence[str] = (),
    ) -> RepairReport:
        """Repair the snapshot, or report why the container could not.

        The returned report distinguishes ``baseline_not_reproduced``,
        ``waiting_for_dependencies``, ``sandbox_error``, ``exhausted`` and a
        verified ``succeeded``; sandbox receipts keep ``build_failed``,
        ``test_failed`` and ``timeout`` visible per run.
        """

        if snapshot.computed_hash() != snapshot.snapshot_hash:
            raise DomainError(
                "REPAIR_SNAPSHOT_CORRUPTED",
                "Snapshot hash does not match its content",
                502,
            )
        request = RepairRequest.from_handoff(
            handoff,
            snapshot,
            verification_files=dict(verification_files),
            verification_tests=verification_tests,
            budget=budget,
            patch_limits=patch_limits,
            frozen_paths=frozen_paths,
        )
        agent = RepairAgent(
            executor=self.executor,
            memory=self.memory,
            clock=self.clock,
        )
        return agent.run(request, provider)
