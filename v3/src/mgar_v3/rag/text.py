import ast
import base64
import io
import json
import os
import re
import subprocess
import sys
from collections import Counter
from functools import lru_cache

import jieba
from pypdf import PdfReader

from mgar_v3.domain import DomainError
from mgar_v3.rag.domain import ParsedChunk


@lru_cache(maxsize=2048)
def terms(text: str) -> tuple[str, ...]:
    tokens = []
    for token in jieba.cut(text[:65536], HMM=False):
        value = token.strip().lower()
        if re.search(r"[\w\u4e00-\u9fff]", value):
            tokens.append(value)
    # Preserve exact code symbols and error codes as well as segmented Chinese words.
    tokens.extend(x.lower() for x in re.findall(r"[A-Za-z_][A-Za-z0-9_.-]*", text[:65536]))
    return tuple(tokens[:8192])


def frequencies(text: str) -> dict[str, int]:
    return dict(Counter(terms(text)))


def _blocks(text: str, page: int | None = None, code: bool = False) -> list[ParsedChunk]:
    lines = text.splitlines()
    if any(len(line.encode()) > 4096 for line in lines):
        raise DomainError("SOURCE_LINE_LIMIT", "A source line exceeds the chunk byte bound", 413)
    symbols = {}
    if code:
        try:
            tree = ast.parse(text)
            symbols = {
                node.lineno: node.name
                for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            }
        except SyntaxError:
            pass
    chunks = []
    buffer: list[str] = []
    start = 1
    size = 0
    symbol = None

    def flush(end: int) -> None:
        nonlocal buffer, size, start, symbol
        content = "\n".join(buffer)
        if content.strip():
            locator: dict[str, int | str] = {"start_line": start, "end_line": end}
            if page is not None:
                locator["page"] = page
            if symbol:
                locator["symbol"] = symbol
            chunks.append(ParsedChunk(text=content, locator=locator))
        buffer = []
        size = 0

    for number, line in enumerate(lines, 1):
        new_section = line.startswith("# ") or number in symbols
        if buffer and (size + len(line.encode()) + 1 > 2048 or new_section):
            flush(number - 1)
        if not buffer:
            start = number
            symbol = symbols.get(number)
        buffer.append(line)
        size += len(line.encode()) + 1
    if buffer:
        flush(len(lines))
    return chunks


def parse_untrusted(content: bytes, media_type: str) -> list[ParsedChunk]:
    if not content or len(content) > 2_000_000:
        raise DomainError("DOCUMENT_SIZE_LIMIT", "Document is empty or exceeds 2 MB", 413)
    try:
        if media_type == "application/pdf":
            reader = PdfReader(io.BytesIO(content), strict=True)
            if reader.is_encrypted or len(reader.pages) > 32:
                raise DomainError("PDF_UNSUPPORTED", "Encrypted PDF or page count is unsupported")
            chunks = []
            total = 0
            for number, page in enumerate(reader.pages, 1):
                text = page.extract_text() or ""
                total += len(text.encode())
                if total > 262144:
                    raise DomainError("PARSED_TEXT_LIMIT", "PDF text exceeds its bound")
                chunks.extend(_blocks(text, number))
            if not chunks:
                raise DomainError("SCANNED_PDF_UNSUPPORTED", "PDF contains no readable text")
        elif media_type in {"text/plain", "text/markdown", "text/x-python", "text/x-code"}:
            text = content.decode("utf-8-sig")
            if "\x00" in text or len(text.encode()) > 262144:
                raise DomainError("PARSED_TEXT_LIMIT", "Text contains NUL or exceeds its bound")
            chunks = _blocks(text, code=media_type in {"text/x-python", "text/x-code"})
        else:
            raise DomainError("DOCUMENT_TYPE_DENIED", "Document type is unsupported", 415)
    except DomainError:
        raise
    except Exception:
        raise DomainError("DOCUMENT_PARSE_FAILED", "Document could not be parsed") from None
    if not chunks or len(chunks) > 128:
        raise DomainError("CHUNK_COUNT_LIMIT", "Document has no chunks or exceeds 128 chunks")
    return chunks


def parse(content: bytes, media_type: str) -> list[ParsedChunk]:
    if media_type != "application/pdf":
        return parse_untrusted(content, media_type)
    if not content or len(content) > 2_000_000:
        raise DomainError("DOCUMENT_SIZE_LIMIT", "Document exceeds its parser byte bound", 413)
    try:
        result = subprocess.run(
            [sys.executable, "-m", "mgar_v3.rag.parser_worker"],
            input=base64.b64encode(content),
            capture_output=True,
            timeout=6,
            env={"PATH": os.defpath, "LANG": "C.UTF-8"},
            check=False,
        )
        if result.returncode != 0 or len(result.stdout) > 1_000_000:
            raise DomainError(
                "DOCUMENT_PARSE_LIMIT", "PDF parser exceeded a process resource bound"
            )
        value = json.loads(result.stdout)
        if "error" in value:
            raise DomainError(value["error"], "PDF parsing was rejected")
        return [ParsedChunk.model_validate(item) for item in value["chunks"]]
    except (subprocess.TimeoutExpired, ValueError):
        raise DomainError(
            "DOCUMENT_PARSE_LIMIT", "PDF parser did not finish within its bound"
        ) from None
