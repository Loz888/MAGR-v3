"""V3.5 end-to-end evaluation harness (V35-E01).

Module layout:

* ``tasks`` loads the pinned development and holdout datasets;
* ``runner`` drives the real Workbench repair chain over the R03 sandbox;
* ``grader`` turns receipts into scored, gate-aware result records.
"""

from __future__ import annotations

from .grader import classify, grade, summarize
from .tasks import Task, load_development, load_holdout, validate_dataset

__all__ = [
    "Task",
    "classify",
    "grade",
    "load_development",
    "load_holdout",
    "summarize",
    "validate_dataset",
]
