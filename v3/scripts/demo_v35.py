"""Materialise the pinned example repository for the V3.5 demo.

The demo defect is real: the snapshot is a byte-exact public repository at a
fixed commit where the documented bug still exists.  This script copies that
snapshot into a disposable directory, verifies its digest, and writes the
natural-language task and reproduction notes next to it.

It never calls a model, never touches Docker and never reaches the network.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from mgar_v3.quality_eval.v35 import tasks as harness

V3_ROOT = Path(__file__).resolve().parents[1]
EXPERIMENT = V3_ROOT / "experiments" / "v35-harness"
DEFAULT_TASK = "v35-adversarial-help-name-retry"
DEFAULT_OUT = V3_ROOT / ".local" / "demo-v35"


def _select(task_id: str) -> harness.Task:
    available = harness.load_development(EXPERIMENT)
    for task in available:
        if task.task_id == task_id:
            return task
    known = ", ".join(item.task_id for item in available)
    raise SystemExit(f"unknown demo task: {task_id}; available: {known}")


def _materialise(task: harness.Task, out: Path, *, force: bool) -> None:
    repo = out / "example-repo"
    if repo.exists():
        if not force:
            raise SystemExit(f"example repository already exists: {repo} (pass --force)")
        shutil.rmtree(repo)
    repo.mkdir(parents=True)
    for source in harness.snapshot_files(task.snapshot_dir):
        relative = source.relative_to(task.snapshot_dir)
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    digest = harness.snapshot_digest(task.snapshot_dir)
    if digest != task.snapshot_digest:
        raise SystemExit("snapshot digest mismatch; the pinned assets were modified")


def _write_notes(task: harness.Task, out: Path) -> None:
    notes = f"""# 示例仓库：{task.repository} @ {task.base_commit}

这是 V3.5 演示用的固定 Commit 快照，内容只读复制自 `v3/experiments/v35-harness`。
脚本已校验快照 SHA-256：`{task.snapshot_digest}`。

## 自然语言任务

{task.prompt}

## 允许修改的路径

{chr(10).join(f"- `{path}`" for path in task.allowed_paths)}

## 复现缺陷

公开测试：{chr(10).join(f"- `{target}`" for target in task.public_targets)}

在执行 `make demo` 后，评测报告会给出基线失败、每轮补丁、公开/隐藏测试回执与
Review Bundle 路径。缺陷不是注入的伪造代码，而是该固定 Commit 上真实存在的缺陷。

## 边界

- 该目录只是演示副本，不参与评测；修改它不会影响 Harness 资产。
- 不调用付费模型、不访问网络、不写入 GitHub。
"""
    (out / "DEFECT.md").write_text(notes, encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", default=DEFAULT_TASK, help="development task id")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--force", action="store_true", help="replace an existing output")
    args = parser.parse_args(argv)

    task = _select(args.task)
    out = args.out.expanduser().resolve()
    _materialise(task, out, force=args.force)
    _write_notes(task, out)
    payload = {
        "task_id": task.task_id,
        "repository": task.repository,
        "base_commit": task.base_commit,
        "snapshot_digest": task.snapshot_digest,
        "example_repo": str(out / "example-repo"),
        "notes": str(out / "DEFECT.md"),
        "model_calls": 0,
        "network": "not_used",
    }
    (out / "demo.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
