import re

import pytest

from mgar_v3.domain import DomainError
from mgar_v3.repair.workspace import apply_diff, fixture_diff, fixture_snapshot, recount_diff


def test_recount_changes_only_declared_counts_and_is_idempotent():
    snapshot = fixture_snapshot("example/mgar-test")
    original = fixture_diff(snapshot, 0)
    malformed = re.sub(r"(@@ -\d+)(?:,\d+)?( \+\d+)(?:,\d+)? @@", r"\1,999\2,999 @@", original)
    with pytest.raises(DomainError):
        apply_diff(snapshot, malformed)
    fixed = recount_diff(malformed)
    assert fixed == original and recount_diff(fixed) == fixed
    assert [line for line in malformed.splitlines(True) if not line.startswith("@@ ")] == [
        line for line in fixed.splitlines(True) if not line.startswith("@@ ")
    ]
    assert apply_diff(snapshot, fixed) == apply_diff(snapshot, original)


def test_recount_cannot_relocate_a_hunk_or_bypass_path_and_ast_checks():
    snapshot = fixture_snapshot("example/mgar-test")
    original = fixture_diff(snapshot, 0)
    match = re.search(r"@@ -(\d+)", original)
    start = int(match[1])
    wrong = original.replace("@@ -" + str(start), "@@ -" + str(start + 1), 1)
    with pytest.raises(DomainError):
        apply_diff(snapshot, recount_diff(wrong))
    with pytest.raises(DomainError):
        apply_diff(snapshot, recount_diff(original.replace("orders.go", "orders_test.go")))
    with pytest.raises(DomainError):
        recount_diff(original + "unsupported\n")
