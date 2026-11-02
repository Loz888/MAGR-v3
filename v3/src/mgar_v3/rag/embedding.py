import hashlib
import json
import math
import threading
from pathlib import Path
from typing import Any, Protocol

from mgar_v3.domain import DomainError
from mgar_v3.rag.text import terms
from mgar_v3.settings import ROOT


class Embedder(Protocol):
    dimension: int
    model_id: str
    mode: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


def normalize(values: list[float], dimension: int) -> list[float]:
    if len(values) != dimension or any(not math.isfinite(v) for v in values):
        raise DomainError("EMBEDDING_DIMENSION", "Embedding dimensions or values are invalid")
    norm = math.sqrt(sum(v * v for v in values))
    if norm < 1e-12:
        raise DomainError("EMBEDDING_EMPTY", "No usable word vectors in the source")
    return [v / norm for v in values]


class FixtureEmbedding:
    dimension = 300
    model_id = "fixture-token-hash-v1:300"
    mode = "fixture"

    def embed(self, texts: list[str]) -> list[list[float]]:
        results = []
        for text in texts:
            values = [0.0] * self.dimension
            for token in terms(text):
                h = hashlib.sha256(token.encode()).digest()
                values[int.from_bytes(h[:4], "big") % self.dimension] += 1.0 if h[4] % 2 else -1.0
            results.append(normalize(values, self.dimension))
        return results


class SpacyEmbedding:
    dimension = 300
    mode = "local_pretrained"

    def __init__(self, path: Path = ROOT / ".local/models/zh_core_web_md-3.8.0"):
        lock = json.loads((ROOT / "config/rag-model-lock.json").read_text())
        receipt = path / "mgar-model-receipt.json"
        if (
            not receipt.is_file()
            or json.loads(receipt.read_text()).get("archive_sha256") != lock["sha256"]
        ):
            raise DomainError("RAG_MODEL_NOT_INSTALLED", "Run make rag-models first", 503)
        recorded = json.loads(receipt.read_text())["files"]
        for name, expected in recorded.items():
            target = path / name
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != expected:
                raise DomainError("RAG_MODEL_CHANGED", "Installed model hash changed", 503)
        try:
            import spacy
        except ImportError:
            raise DomainError(
                "RAG_RUNTIME_MISSING", "Install the rag-models optional dependencies", 503
            ) from None
        self._nlp: Any = spacy.load(
            path,
            exclude=[
                "tok2vec",
                "tagger",
                "parser",
                "attribute_ruler",
                "ner",
                "senter",
                "lemmatizer",
            ],
        )
        if self._nlp.vocab.vectors_length != self.dimension:
            raise DomainError(
                "EMBEDDING_DIMENSION", "Installed vector model has a different dimension"
            )
        self._mutex = threading.Lock()
        self.model_id = "zh_core_web_md@3.8.0:mean300:l2:" + lock["sha256"]

    def embed(self, texts: list[str]) -> list[list[float]]:
        results = []
        with self._mutex:
            for text in texts:
                doc = self._nlp.make_doc(text[:65536])
                vectors = [token.vector for token in doc if token.has_vector and not token.is_space]
                if not vectors:
                    raise DomainError("EMBEDDING_EMPTY", "Text has no known word vectors")
                values = [
                    sum(float(v[i]) for v in vectors) / len(vectors) for i in range(self.dimension)
                ]
                results.append(normalize(values, self.dimension))
        return results
