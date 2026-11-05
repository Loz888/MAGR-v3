"""Public request and evidence checks. Never inspect hidden labels or expected answers."""

import re
from decimal import Decimal

from mgar_v3.benchmark.domain import Answer, Case, Source

ALLOWED_CAPABILITIES = {"read", "local_patch_proposal"}
DECIMAL = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?$")


def capability_decision(case: Case) -> Answer | None:
    if not set(case.requested_capabilities) <= ALLOWED_CAPABILITIES:
        return Answer(
            status="denied", explanation="Requested capabilities exceed this read-only Run."
        )
    return None


def evidence_decision(case: Case, sources: list[Source], errors: list[str]) -> Answer | None:
    if case.output_contract is None:
        return None
    if "knowledge:KNOWLEDGE_REVOKED" in errors:
        return Answer(status="denied", explanation="The requested knowledge access was revoked.")
    if set(case.required_roles) - {s.role for s in sources}:
        return Answer(status="insufficient", explanation="Required readable evidence is missing.")
    knowledge = [s for s in sources if s.role == "knowledge"]
    if (
        "knowledge" in case.required_roles
        and case.document_id
        and not any(s.document_id == case.document_id for s in knowledge)
    ):
        return Answer(
            status="insufficient", explanation="The requested document was not retrieved."
        )
    if case.required_revision and any(s.revision != case.required_revision for s in knowledge):
        return Answer(
            status="insufficient", explanation="Required knowledge revision does not match."
        )
    return None


def output_issues(answer: Answer, case: Case, sources: list[Source]) -> list[str]:
    issues = []
    available = {s.id: s for s in sources}
    cited = set()
    for ref in answer.citations:
        source = available.get(ref.source_id)
        if not source or not ref.quote or ref.quote not in source.text:
            issues.append("invalid_citation")
        else:
            cited.add(source.role)
    if answer.status != "supported":
        if answer.values or answer.diff is not None:
            issues.append("non_supported_must_have_empty_values_and_null_diff")
        return issues
    if not set(case.required_roles) <= cited:
        issues.append("missing_source_role")
    if case.category == "repair":
        if not answer.diff or answer.values:
            issues.append("repair_requires_diff_and_empty_values")
    elif answer.diff is not None:
        issues.append("unexpected_diff")
    contract = case.output_contract
    if contract is not None:
        if set(answer.values) != set(contract.fields):
            issues.append("wrong_value_fields")
        for name, kind in contract.fields.items():
            value = answer.values.get(name, "")
            if kind == "boolean" and value not in {"true", "false"}:
                issues.append("canonical_boolean_required:" + name)
            if kind == "decimal" and not DECIMAL.fullmatch(value):
                issues.append("decimal_string_required:" + name)
    for source in sources:
        if source.role != "data":
            continue
        for key, fact in source.numeric_facts.items():
            if key in answer.values:
                try:
                    equal = Decimal(answer.values[key]) == Decimal(fact)
                except ArithmeticError:
                    equal = False
                if not equal:
                    issues.append("numeric_evidence_conflict:" + key)
    return sorted(set(issues))


def safe_failure(issues: list[str]) -> Answer:
    return Answer(
        status="insufficient", explanation="Final output checks failed: " + ", ".join(issues)
    )
