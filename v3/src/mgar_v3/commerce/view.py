"""Versioned presentation rules; callers supply server-derived facts, never authority."""

import json
from pathlib import Path
from typing import Any

RULES = json.loads(Path(__file__).with_name("status-rules.json").read_text())


def matches(value: dict[str, Any], rule: dict[str, Any]) -> bool:
    if "all" in rule:
        return all(matches(value, c) for c in rule["all"])
    if "any" in rule:
        return any(matches(value, c) for c in rule["any"])
    try:
        actual: Any = value
        for key in rule["path"].split("."):
            actual = actual[key]
    except (KeyError, TypeError):
        return False
    if "equals" in rule:
        return bool(actual == rule["equals"])
    if "not_equals" in rule:
        return bool(actual != rule["not_equals"])
    return actual in rule["in"]


def render(value: dict[str, Any]) -> dict[str, Any]:
    label = next(
        (r["code"] for r in RULES["rules"] if matches(value, r["when"])), RULES["fallback"]["code"]
    )
    return {
        "status": label,
        **{key: matches(value, guard) for key, guard in RULES["guards"].items()},
    }
