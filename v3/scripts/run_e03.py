"""Run the V3.5 E03 end-to-end security and recovery scenarios.

The command executes the pytest node ids registered in
``mgar_v3.quality_eval.v35.security`` with JUnit XML and writes one immutable
scenario report.  Each record carries the reproducible command, failure point,
observed state, recovery result and the real test outcome; unavailable
scenarios are stored as ``NOT_RUN`` with a reason.
"""

from __future__ import annotations

from mgar_v3.quality_eval.v35.security import main

if __name__ == "__main__":
    raise SystemExit(main())
