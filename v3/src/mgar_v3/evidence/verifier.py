from uuid import UUID

from mgar_v3.coordination.engine import Engine
from mgar_v3.domain import DomainError, Investigation, Verification


def verify(engine: Engine, run_id: UUID, result: Investigation) -> Verification:
    reasons = []
    checks = []
    for claim in result.claims:
        row = engine.get_evidence(run_id, claim.evidence_id)
        if row is None:
            reasons.append("evidence_not_read_by_this_run")
            continue
        try:
            content = engine.artifacts.read(row["content_hash"]).decode()
        except (DomainError, UnicodeDecodeError):
            reasons.append("source_bytes_invalid")
            continue
        lines = content.splitlines()
        start, end = row["start_line"], row["end_line"]
        if not (1 <= start <= end <= len(lines)):
            reasons.append("source_locator_invalid")
            continue
        if claim.quote not in "\n".join(lines[start - 1 : end]):
            reasons.append("quotation_not_in_read_source")
        elif claim.statement != claim.quote:
            # R1 proves direct source quotations only. Inference requires later evaluation.
            reasons.append("statement_exceeds_mechanically_verified_quote")
        else:
            checks.append(str(claim.evidence_id) + ":read+hash+locator+quote")
    return Verification(
        passed=not reasons and len(checks) == len(result.claims), checks=checks, reasons=reasons
    )
