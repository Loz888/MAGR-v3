"""V3.5 D01 documentation, demo and aggregate acceptance checks.

The card is a delivery card: the product description, the 90-second demo, the
evaluation index and the Makefile entry points must stay executable and honest.
These tests do not call a model, do not reach the network and do not need
Docker; the real Docker run is exercised by ``make demo`` and recorded in the
D01 acceptance evidence.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from mgar_v3.quality_eval.v35 import tasks as harness

V3_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = V3_ROOT.parent
DOCS = V3_ROOT / "docs" / "v35"
ACCEPT = DOCS / "accept"


def _task(task_id: str) -> harness.Task:
    return next(
        item
        for item in harness.load_development(V3_ROOT / "experiments" / "v35-harness")
        if item.task_id == task_id
    )


def test_product_document_covers_required_sections() -> None:
    text = (DOCS / "PRODUCT.md").read_text(encoding="utf-8")
    for marker in (
        "快速启动",
        "Web / CLI 双入口",
        "任务生命周期",
        "授权模型",
        "唯一契约",
        "与 V3.2 / V3.3 / V3.4 的能力差异",
        "Live / Fixture 区分",
        "成本模型",
        "安全模型",
        "已实现与已知边界",
        "可复现命令",
    ):
        assert marker in text
    assert "make deps-up" in text
    assert "mgar workbench" in text
    assert "https://github.com/Loz888/MAGR-v3.git" in text


def test_demo_script_contains_the_seven_stage_narrative() -> None:
    text = (DOCS / "DEMO.md").read_text(encoding="utf-8")
    for marker in (
        "Finding/Evidence",
        "RepairHandoff",
        "PatchPlan",
        "测试回执",
        "Review Bundle",
        "waiting_approval",
        "Draft PR",
        "make demo-repo",
        "make demo",
        "/workbench",
    ):
        assert marker in text
    assert "live_agent_gate" in text
    assert "NOT_RUN" in text


def test_evaluation_index_matches_acceptance_gates() -> None:
    index = (DOCS / "EVALUATION_INDEX.md").read_text(encoding="utf-8")
    expected = {
        "V35-E01": ("PASS", "NOT_RUN", "NOT_RUN", "NOT_RUN", "PASS"),
        "V35-E02": ("PASS", "FAIL", "NOT_RUN", "FAIL", "PASS"),
        "V35-E03": ("PASS", "NOT_RUN", "NOT_RUN", "NOT_RUN", "PASS"),
    }
    for card, gates in expected.items():
        accept = json.loads((ACCEPT / card / "accept.json").read_text(encoding="utf-8"))
        actual = (
            accept["engineering_gate"],
            accept["live_model_gate"],
            accept["live_github_write_gate"],
            accept["live_agent_gate"],
            accept["security_gate"],
        )
        assert actual == gates, card
        row = next(
            line for line in index.splitlines() if line.startswith(f"| {card} |")
        )
        assert "**FAIL**" in row if "FAIL" in gates else "FAIL" not in row
        assert f"./accept/{card}/accept.json" in row
        assert card in index


def test_documentation_links_exist() -> None:
    root = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    guide = (V3_ROOT / "README.md").read_text(encoding="utf-8")
    for text in (root, guide):
        assert "v3/docs/v35/PRODUCT.md" in text or "docs/v35/PRODUCT.md" in text
        assert "v3/docs/v35/DEMO.md" in text or "docs/v35/DEMO.md" in text
        assert "V3.5" in text
    assert "make demo" in root and "make demo" in guide
    assert "/workbench" in guide


def test_makefile_demo_targets_are_fixture_and_repeatable() -> None:
    makefile = (V3_ROOT / "Makefile").read_text(encoding="utf-8")
    assert re.search(r"^demo:", makefile, flags=re.MULTILINE)
    assert re.search(r"^demo-repo:", makefile, flags=re.MULTILINE)
    demo = next(line for line in makefile.splitlines() if "run_eval.py" in line)
    assert "--mode fixture" in demo
    assert "--task v35-adversarial-help-name-retry" in demo
    assert "date -u" in demo
    assert "demo_v35.py" in makefile


def test_demo_repository_is_a_pinned_snapshot(tmp_path: Path) -> None:
    task = _task("v35-adversarial-help-name-retry")
    out = tmp_path / "demo"
    result = subprocess.run(
        [sys.executable, "scripts/demo_v35.py", "--out", str(out)],
        cwd=V3_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["task_id"] == task.task_id
    assert payload["base_commit"] == task.base_commit
    assert payload["snapshot_digest"] == task.snapshot_digest
    assert payload["model_calls"] == 0
    assert payload["network"] == "not_used"
    repo = out / "example-repo"
    assert (repo / "src" / "click" / "core.py").is_file()
    assert harness.snapshot_digest(repo) == task.snapshot_digest
    notes = (out / "DEFECT.md").read_text(encoding="utf-8")
    assert task.prompt in notes
    assert task.base_commit in notes
    assert "不调用付费模型" in notes


def test_demo_repository_refuses_to_overwrite(tmp_path: Path) -> None:
    out = tmp_path / "demo"
    argv = [sys.executable, "scripts/demo_v35.py", "--out", str(out)]
    first = subprocess.run(argv, cwd=V3_ROOT, capture_output=True, text=True, timeout=120)
    assert first.returncode == 0, first.stderr
    second = subprocess.run(argv, cwd=V3_ROOT, capture_output=True, text=True, timeout=120)
    assert second.returncode != 0
    assert "already exists" in second.stderr


def test_d01_is_registered_in_verification() -> None:
    config = json.loads((V3_ROOT / "config" / "verification.json").read_text(encoding="utf-8"))
    entry = config["V35-D01"]
    assert entry["tests"] == ["tests/test_v35_d01.py"]
    assert 0 < entry["timeout_seconds"] <= 600
    assert entry["components"]["model"] == "not_used"
    assert entry["components"]["github"] == "not_used"


def test_task_queue_and_evaluation_accept_directories_are_consistent() -> None:
    queue = (DOCS / "TASK_QUEUE.md").read_text(encoding="utf-8")
    row = next(line for line in queue.splitlines() if line.startswith("| V35-D01 |"))
    assert "DONE" in row
    for card in ("V35-E01", "V35-E02", "V35-E03"):
        accept = json.loads((ACCEPT / card / "accept.json").read_text(encoding="utf-8"))
        assert accept["status"] == "PASS"


def test_no_live_claim_is_made_by_the_demo_or_docs() -> None:
    for name in ("PRODUCT.md", "DEMO.md", "EVALUATION_INDEX.md"):
        text = (DOCS / name).read_text(encoding="utf-8")
        assert "live_agent_gate" in text
    demo = (DOCS / "DEMO.md").read_text(encoding="utf-8")
    assert "live_agent_gate=NOT_RUN" in demo or "live_agent_gate` / `live_github_write_gate" in demo
    demo_script = (V3_ROOT / "scripts" / "demo_v35.py").read_text(encoding="utf-8")
    assert "holdout" not in demo_script.lower()
