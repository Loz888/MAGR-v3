from uuid import uuid4

from mgar_v3.domain import Claim, Investigation
from mgar_v3.evidence.acceptance import r1_coverage

SOURCE_HASH = "f025af6ede4dc6dbff19e4e2cf5ee877e73b6fb746ac106a3220923c8011f4d2"
FACTS = [
    "Thread、Mandate、Ticket 和 Run",
    "单任务 LangGraph",
    "payload hash",
    "Action/Outbox/Dispatcher",
    "不重复发布",
]


def test_independent_oracle_requires_all_distinct_business_facts():
    claims = [Claim(statement=fact, quote=fact, evidence_id=uuid4()) for fact in FACTS]
    assert r1_coverage(Investigation(claims=claims), SOURCE_HASH)["passed"]
    assert not r1_coverage(Investigation(claims=claims[:-1]), SOURCE_HASH)["passed"]
    assert not r1_coverage(Investigation(claims=claims), "0" * 64)["passed"]


def test_duplicate_full_source_quotes_do_not_replace_independent_coverage():
    quote = ";".join(FACTS)
    claims = [Claim(statement=quote, quote=quote, evidence_id=uuid4()) for _ in range(5)]
    assert not r1_coverage(Investigation(claims=claims), SOURCE_HASH)["passed"]
