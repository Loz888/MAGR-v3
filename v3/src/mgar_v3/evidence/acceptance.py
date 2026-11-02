import json
from pathlib import Path
from typing import Any

from mgar_v3.domain import Investigation
from mgar_v3.settings import ROOT


def r1_coverage(
    finding: Investigation,
    source_hash: str,
    oracle_path: Path = ROOT / "config/r1-live-oracle.json",
) -> dict[str, Any]:
    oracle = json.loads(oracle_path.read_text())
    quotes = list(dict.fromkeys(claim.quote for claim in finding.claims))
    anchors = oracle["required_anchors"]
    assigned: dict[int, int] = {}

    def match(anchor: int, seen: set[int]) -> bool:
        for index, quote in enumerate(quotes):
            if anchors[anchor] not in quote or index in seen:
                continue
            seen.add(index)
            if index not in assigned or match(assigned[index], seen):
                assigned[index] = anchor
                return True
        return False

    matched = sum(match(index, set()) for index in range(len(anchors)))
    passed = (
        source_hash == oracle["source_hash"]
        and len(quotes) >= oracle["minimum_distinct_quotes"]
        and matched == len(anchors)
    )
    return {
        "passed": passed,
        "oracle_version": oracle["version"],
        "distinct_quotes": len(quotes),
        "required_facts": len(anchors),
        "matched_facts": matched,
        "source_hash_matches": source_hash == oracle["source_hash"],
    }
