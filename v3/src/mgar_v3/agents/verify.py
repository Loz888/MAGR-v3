"""Independent, non-LLM verification of actually read R4 observations."""

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from mgar_v3.agents.domain import Fact, Finding, Resolution
from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, Verification, digest


def cell_text(column: str, value: Any, oid: int) -> str:
    return f"SQL[{column}] = {json.dumps(value, ensure_ascii=False)} (OID {oid})"


def verify_findings(
    engine: Engine, run_id: UUID, findings: list[Finding], required: list[str]
) -> tuple[Resolution, Verification]:
    reasons: list[str] = []
    verified: list[Fact] = []
    missing: list[str] = []
    versions: dict[str, set[str]] = {}
    completed: list[Any] = []
    for finding in findings:
        if finding.status == "succeeded" and finding.facts:
            completed.append(finding.role)
        else:
            missing.extend(
                finding.open_questions or [f"{finding.role}: source evidence unavailable"]
            )
        for fact in finding.facts:
            try:
                engine.tools.check_evidence(fact.evidence_id)
                with engine.transaction() as conn:
                    evidence = conn.execute(
                        (
                            "SELECT e.*,r.role,r.task_id,r.origin FROM app.evidence e JOIN "
                            "app.r4_reads r USING(evidence_id) WHERE e.evidence_id=%s AND "
                            "e.run_id=%s"
                        ),
                        (fact.evidence_id, run_id),
                    ).fetchone()
                if (
                    not evidence
                    or evidence["role"] != finding.role
                    or evidence["task_id"] != finding.task_id
                ):
                    raise DomainError("READ_PROVENANCE", "Evidence was not read by this assignment")
                content = engine.artifacts.read(evidence["content_hash"]).decode()
                if evidence["start_line"] != 1 or evidence["end_line"] != max(
                    1, len(content.splitlines())
                ):
                    raise DomainError(
                        "READ_LOCATOR", "Evidence line locator differs from actual read bytes"
                    )
                origin = evidence["origin"]
                if finding.role == "data":
                    result = json.loads(content)
                    if result["result_hash"] != digest(
                        {k: v for k, v in result.items() if k != "result_hash"}
                    ):
                        raise DomainError("SQL_HASH", "SQL result digest changed")
                    age = (
                        datetime.now(UTC) - datetime.fromisoformat(result["observed_at"])
                    ).total_seconds()
                    if not 0 <= age <= engine.settings.r4_sql_ttl_seconds:
                        raise DomainError("SQL_STALE", "SQL observation is stale")
                    cell = fact.cell
                    if cell is None or set(cell) != {"row", "column", "value", "type_oid"}:
                        raise DomainError("SQL_CELL", "Numeric evidence needs a typed cell")
                    row, col = cell["row"], cell["column"]
                    if type(row) is not int or type(col) is not int or row < 0 or col < 0:
                        raise DomainError("SQL_CELL", "SQL cell index is invalid")
                    actual = result["rows"][row][col]
                    column = result["columns"][col]
                    if (
                        type(cell["value"]) is not type(actual)
                        or cell["value"] != actual
                        or type(cell["type_oid"]) is not int
                        or cell["type_oid"] != column["type_oid"]
                        or fact.text != cell_text(column["name"], actual, column["type_oid"])
                        or fact.quote is not None
                    ):
                        raise DomainError(
                            "SQL_CELL", "Claim differs from the actual typed SQL cell"
                        )
                else:
                    if (
                        not fact.quote
                        or fact.quote not in content
                        or fact.text != fact.quote
                        or fact.cell
                    ):
                        raise DomainError(
                            "QUOTE_MISMATCH", "Observation must exactly quote its read source"
                        )
                    if origin["kind"] == "knowledge":
                        versions.setdefault(origin["document_id"], set()).add(
                            evidence["source_version"]
                        )
                verified.append(fact)
            except (DomainError, ValueError, KeyError, IndexError, TypeError) as exc:
                reasons.append(exc.code if isinstance(exc, DomainError) else "EVIDENCE_SHAPE")
    contradictions = [
        f"Document {doc} has conflicting source versions: {', '.join(sorted(v))}"
        for doc, v in versions.items()
        if len(v) > 1
    ]
    missing.extend(
        f"{role}: no verified expert result" for role in required if role not in completed
    )
    # Preserve observations from every branch; no reviewer can erase a mechanical failure.
    resolution = Resolution(
        verified_facts=verified[:24],
        hypotheses=[],
        contradictions=contradictions[:8],
        missing_evidence=list(dict.fromkeys(missing))[:12],
        next_steps=(
            ["Resolve missing or conflicting sources and ask a narrower follow-up."]
            if missing or contradictions
            else ["Review these source observations before drawing a causal conclusion."]
        ),
        completed_roles=list(dict.fromkeys(completed)),
        complete=not reasons and not missing and not contradictions,
    )
    return resolution, Verification(
        passed=not reasons,
        reasons=sorted(set(reasons)),
        checks=[
            "assignment_read_provenance",
            "current_source_acl",
            "artifact_sha256",
            "exact_quote",
            "typed_sql_cell",
            "source_version_conflicts",
            "sql_observation_age",
        ],
    )


def render(
    resolution: Resolution, verdict: Verification, revision: int, modes: dict[str, Any]
) -> bytes:
    lines = [
        "# MGAR investigation",
        "",
        f"Input revision: {revision}",
        f"Mechanical evidence verification: {'PASS' if verdict.passed else 'FAIL'}",
        "Execution components: " + json.dumps(modes, ensure_ascii=False, sort_keys=True),
    ]
    for title, values in [
        (
            "Verified observations",
            [f"{f.text} [evidence:{f.evidence_id}]" for f in resolution.verified_facts],
        ),
        ("Hypotheses", resolution.hypotheses),
        ("Contradictions", resolution.contradictions),
        ("Missing evidence", resolution.missing_evidence),
        ("Next steps", resolution.next_steps),
    ]:
        lines.extend(["", "## " + title, *["- " + v for v in values]])
    lines.extend(
        [
            "",
            (
                "Scope: source observations and mechanical checks; causal correctness "
                "is not established."
            ),
        ]
    )
    return "\n".join(lines).encode()
