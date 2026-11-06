from datetime import datetime
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from mgar_v3.domain import Model

Kind = Literal["duplicate_paid", "status_filter", "refunded_status"]
Status = Literal["passed", "failed", "needs_input", "denied", "unknown"]
Hash = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Key = Annotated[str, Field(min_length=1, max_length=200)]
Money = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)\.[0-9]{2}$")]
Count = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)$")]
Code = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,79}$")]


class Window(Model):
    start: datetime
    end: datetime

    @model_validator(mode="after")
    def bounded(self) -> Self:
        if self.start.tzinfo is None or self.end.tzinfo is None or self.start >= self.end:
            raise ValueError("A timezone-aware nonempty half-open window is required")
        if (self.end - self.start).total_seconds() > 31 * 86400:
            raise ValueError("Commerce windows are bounded to 31 days")
        return self


class CaseInput(Model):
    template_id: Kind
    issue_number: Annotated[int, Field(strict=True, gt=0)]
    window: Window
    goal: Literal["report", "draft_pr"] = "report"
    client_request_id: Key
    execution_mode: Literal["fixture", "live"] = "fixture"


class CaseChanges(Model):
    window: Window | None = None
    goal: Literal["report", "draft_pr"] | None = None


class CaseMessage(Model):
    body: Annotated[str, Field(min_length=1, max_length=4000)]
    client_request_id: Key
    expected_case_revision: Annotated[int, Field(strict=True, gt=0)]
    changes: CaseChanges | None = None


class RepairRequest(Model):
    expected_case_revision: Annotated[int, Field(strict=True, gt=0)]
    investigation_result_hash: Hash
    client_request_id: Key


class Check(Model):
    status: Literal["passed", "failed", "unknown", "not_run", "not_applicable"]
    reason_codes: list[Code] = Field(default_factory=list, max_length=32)


class Checks(Model):
    scope: Check
    evidence: Check
    contract: Check
    acceptance: Check


class Metrics(Model):
    paid_events: Count
    distinct_orders: Count
    event_amount: Money
    dedup_amount: Money
    inflation_amount: Money


class QuoteRef(Model):
    source_id: UUID
    content_hash: Hash
    unit_kind: Literal["knowledge_line", "code_lines"]
    start_line: Annotated[int, Field(strict=True, gt=0)]
    end_line: Annotated[int, Field(strict=True, gt=0)]
    unit_hash: Hash


class TestRef(Model):
    kind: Literal["public", "independent"]
    receipt_hash: Hash
    status: Literal["passed", "failed", "unknown"]
    skipped: Annotated[int, Field(strict=True, ge=0)]


class BusinessResult(Model):
    schema_version: Literal["commerce-business-result-v1"] = Field(
        default="commerce-business-result-v1", alias="schema"
    )
    result_id: UUID
    case_id: UUID
    case_revision: Annotated[int, Field(strict=True, gt=0)]
    stage: Literal["investigation", "repair_proposal"]
    run_id: UUID
    input_revision: Annotated[int, Field(strict=True, gt=0)]
    execution_epoch: Annotated[int, Field(strict=True, gt=0)]
    spec_hash: Hash
    checker_version: Literal["commerce-checker-v1.0"] = "commerce-checker-v1.0"
    created_at: datetime
    business_status: Status
    checks: Checks
    reason_codes: Annotated[list[Code], Field(max_length=32)]
    missing_inputs: Annotated[
        list[Annotated[str, Field(min_length=1, max_length=1000)]], Field(max_length=16)
    ]
    artifact_hash: Hash | None
    facts: dict[str, str]
    quote_refs: Annotated[list[QuoteRef], Field(max_length=24)]
    numeric_receipt_hash: Hash | None
    test_receipt_refs: Annotated[list[TestRef], Field(max_length=2)]
    parent_investigation_result_hash: Hash | None

    @model_validator(mode="after")
    def coherent(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("Result time must include a timezone")
        if any(q.end_line < q.start_line for q in self.quote_refs):
            raise ValueError("Quote line intervals must be nonempty")
        if len(set(self.reason_codes)) != len(self.reason_codes) or len(
            set(self.missing_inputs)
        ) != len(self.missing_inputs):
            raise ValueError("Reasons and missing inputs must be unique")
        if self.stage == "investigation" and (
            self.parent_investigation_result_hash or self.test_receipt_refs
        ):
            raise ValueError("Investigation results have no repair test receipts or parent")
        if self.business_status == "passed":
            if (
                any(
                    c.status != "passed" or c.reason_codes
                    for c in (
                        self.checks.scope,
                        self.checks.evidence,
                        self.checks.contract,
                        self.checks.acceptance,
                    )
                )
                or not self.artifact_hash
                or self.reason_codes
                or self.missing_inputs
            ):
                raise ValueError("Passed results require every mandatory gate")
            if self.stage == "investigation":
                Metrics.model_validate(self.facts)
                if {q.unit_kind for q in self.quote_refs} != {"knowledge_line", "code_lines"}:
                    raise ValueError("Knowledge and code quote units are required")
                if not self.numeric_receipt_hash:
                    raise ValueError("Numeric provenance is required")
            elif (
                len(self.test_receipt_refs) != 2
                or {t.kind for t in self.test_receipt_refs} != {"public", "independent"}
                or any(t.status != "passed" or t.skipped for t in self.test_receipt_refs)
            ):
                raise ValueError("Both protected test suites must pass without skips")
        elif self.facts or not self.reason_codes:
            raise ValueError("Non-passed results require reasons and empty supported facts")
        if self.business_status == "needs_input" and not self.missing_inputs:
            raise ValueError("Missing inputs must be stated")
        if self.business_status in {"unknown", "denied"} and (
            self.artifact_hash
            or self.quote_refs
            or self.numeric_receipt_hash
            or self.test_receipt_refs
        ):
            raise ValueError("Unknown or denied results cannot expose evidence")
        if self.stage == "repair_proposal" and (
            self.facts or not self.parent_investigation_result_hash
        ):
            raise ValueError("Repairs bind the prior investigation and have no report values")
        return self

    def wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class QuoteSelection(Model):
    quote_ids: Annotated[list[str], Field(min_length=1, max_length=2)]
    explanation: Annotated[str, Field(max_length=1200)] = ""
