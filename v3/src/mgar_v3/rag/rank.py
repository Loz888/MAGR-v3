import hashlib
import json
import math
from pathlib import Path
from typing import Any

from mgar_v3.domain import DomainError, digest
from mgar_v3.rag.embedding import Embedder
from mgar_v3.rag.text import terms


def bm25(query: str, documents: list[dict[str, Any]]) -> list[tuple[str, float]]:
    query_terms = set(terms(query))
    n = len(documents)
    average = sum(d["token_count"] for d in documents) / max(n, 1)
    results = []
    for doc in documents:
        score = 0.0
        for token in query_terms:
            tf = doc["terms"].get(token, 0)
            if not tf:
                continue
            df = sum(token in other["terms"] for other in documents)
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            score += (
                idf * tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * doc["token_count"] / max(average, 1)))
            )
        results.append((str(doc["chunk_id"]), score))
    return sorted(results, key=lambda x: (-x[1], x[0]))


def rrf(*rankings: list[tuple[str, float]], k: int = 60) -> list[tuple[str, float]]:
    scores: dict[str, float] = {}
    for ranking in rankings:
        for rank, (key, _) in enumerate(ranking, 1):
            scores[key] = scores.get(key, 0) + 1 / (k + rank)
    return sorted(scores.items(), key=lambda x: (-x[1], x[0]))


def features(query: str, text: str, qvec: list[float], dvec: list[float]) -> list[float]:
    qterms = set(terms(query))
    dterms = set(terms(text))
    overlap = len(qterms & dterms) / max(1, len(qterms))
    cosine = sum(a * b for a, b in zip(qvec, dvec, strict=True))
    return [
        cosine,
        overlap,
        float(query.lower() in text.lower()),
        min(len(text), 4096) / 4096,
        *[a * b for a, b in zip(qvec, dvec, strict=True)],
    ]


class PairwiseRanker:
    def __init__(self, weights: list[float], model_id: str, mode: str):
        if not weights or any(not math.isfinite(w) for w in weights):
            raise DomainError("RERANKER_INVALID", "Ranking model weights are invalid")
        self.weights, self.model_id, self.mode = weights, model_id, mode

    @classmethod
    def fixture(cls, dimension: int = 300) -> "PairwiseRanker":
        return cls([1, 1, 0.2, 0] + [0] * dimension, "fixture-ranker-v1", "fixture")

    @classmethod
    def load(cls, path: Path, embedder: Embedder) -> "PairwiseRanker":
        if not path.is_file():
            raise DomainError("RERANKER_MISSING", "Train the local ranking model first", 503)
        value = json.loads(path.read_text())
        if (
            value["embedding_model"] != embedder.model_id
            or len(value["weights"]) != embedder.dimension + 4
        ):
            raise DomainError("RERANKER_MODEL_MISMATCH", "Ranker and embedding model differ")
        return cls(
            value["weights"],
            "pairwise-linear:" + hashlib.sha256(path.read_bytes()).hexdigest(),
            "local_trained_pairwise",
        )

    def score(self, values: list[float]) -> float:
        if len(values) != len(self.weights):
            raise DomainError("RERANKER_DIMENSION", "Ranking feature dimension differs")
        return sum(w * x for w, x in zip(self.weights, values, strict=True))


def train(pairs: list[dict[str, str]], embedder: Embedder, output: Path) -> dict[str, Any]:
    if not 8 <= len(pairs) <= 256:
        raise DomainError("RANK_TRAINING_BOUND", "Training needs 8 to 256 independent pairs")
    training = []
    for pair in pairs:
        q, p, n = embedder.embed([pair["query"], pair["positive"], pair["negative"]])
        pos = features(pair["query"], pair["positive"], q, p)
        neg = features(pair["query"], pair["negative"], q, n)
        training.append([a - b for a, b in zip(pos, neg, strict=True)])
    weights = [0.0] * (embedder.dimension + 4)
    for _ in range(160):
        for diff in training:
            margin = sum(w * x for w, x in zip(weights, diff, strict=True))
            gradient = 1 / (1 + math.exp(max(-30, min(30, margin))))
            weights = [
                w + 0.08 * (gradient * x - 0.002 * w) for w, x in zip(weights, diff, strict=True)
            ]
    value = {
        "schema": "pairwise-linear-v1",
        "embedding_model": embedder.model_id,
        "weights": weights,
        "training_sha256": digest(pairs),
        "pair_count": len(pairs),
        "epochs": 160,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    return value
