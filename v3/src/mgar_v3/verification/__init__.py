"""V3.3 A04 independent defect verification."""

from mgar_v3.verification.receipts import (
    ExecutionClassification,
    ExecutionReceipt,
    ReceiptJournal,
    classify_execution,
    execution_receipt,
)
from mgar_v3.verification.verifier import (
    STATIC_EVIDENCE_KINDS,
    BaselineRun,
    CommandRunner,
    DefectVerifier,
    ReproductionPlan,
    VerificationEnvironment,
    VerificationOutcome,
    VerificationStatus,
    VerificationSummary,
)

__all__ = [
    "STATIC_EVIDENCE_KINDS",
    "BaselineRun",
    "CommandRunner",
    "DefectVerifier",
    "ExecutionClassification",
    "ExecutionReceipt",
    "ReceiptJournal",
    "ReproductionPlan",
    "VerificationEnvironment",
    "VerificationOutcome",
    "VerificationStatus",
    "VerificationSummary",
    "classify_execution",
    "execution_receipt",
]
