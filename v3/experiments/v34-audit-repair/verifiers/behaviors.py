"""Offline behavioral verifiers for E01 review tasks.

Each function accepts the experiment directory and a task mapping and raises
``AssertionError`` when the pinned buggy snapshot does not exhibit the defect
described by the gold finding. These checks are part of the harness
self-test, not a model capability result.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, Mapping

from mgar_v3.quality_eval import v34

_SENTINEL = """
import copy
import pickle
from click._utils import UNSET

assert copy.copy(UNSET) is UNSET
assert copy.deepcopy(UNSET) is UNSET
assert pickle.loads(pickle.dumps(UNSET)) is UNSET
"""

_SHORT_HELP = """
from click.utils import _make_default_short_help as short

assert short("Weigh apples vs. pears.", 30) == "Weigh apples vs. pears."
assert short("123 567 9. aaaa bbb", 10) == "123 567..."
assert short("well-behaved.", 8) == "..."
assert short("123 567 9.", 10) == "123 567 9."
"""

_HELP_NAME = """
import click
from click.testing import CliRunner

@click.command()
@click.argument("help")
def cli(help):
    click.echo(help)

result = CliRunner().invoke(cli, ["value"])
assert result.exit_code == 0, result.output
assert result.output == "value\\n"
option = cli.get_help_option(click.Context(cli))
assert option is not None
assert option.name == "_click_default_help"
"""


def _expect_buggy_failure(experiment_dir: Path, task: Mapping[str, Any], code: str, label: str) -> list[str]:
    snapshot = experiment_dir / str(task["snapshot_dir"]) / "src"
    result = v34.run_python_snippet(code, snapshot)
    if result.returncode == 0:
        raise AssertionError(f"{task['task_id']}: buggy snapshot unexpectedly passes {label}")
    return [f"buggy behavior reproduced: {label}"]


def check_sentinel_identity(experiment_dir: Path, task: Mapping[str, Any]) -> list[str]:
    return _expect_buggy_failure(experiment_dir, task, _SENTINEL, "Sentinel copy/pickle identity")


def check_short_help_abbreviation(experiment_dir: Path, task: Mapping[str, Any]) -> list[str]:
    return _expect_buggy_failure(experiment_dir, task, _SHORT_HELP, "short help abbreviation")


def check_help_name_collision(experiment_dir: Path, task: Mapping[str, Any]) -> list[str]:
    return _expect_buggy_failure(experiment_dir, task, _HELP_NAME, "argument named help")


def _load_content_type(path: Path) -> Any:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_parse_content_type_header"
    )
    module = ast.Module(body=[node], type_ignores=[])
    namespace: dict[str, Any] = {"re": re}
    exec(compile(module, str(path), "exec"), namespace)
    return namespace["_parse_content_type_header"]


def check_content_type_parameter(experiment_dir: Path, task: Mapping[str, Any]) -> list[str]:
    path = experiment_dir / str(task["snapshot_dir"]) / "src/requests/utils.py"
    function = _load_content_type(path)
    content_type, parameters = function("text/html; charset")
    assert content_type == "text/html", (content_type, parameters)
    assert parameters == {"charset": True}, parameters
    return ["buggy behavior reproduced: parameter without '=' becomes True"]
