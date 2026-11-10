"""Incremental provider SSE decoding and public report previews (never model reasoning)."""

from __future__ import annotations

import codecs
import json
import time
from collections.abc import Callable
from typing import Any

import httpx

MAX_RESPONSE_BYTES = 512 * 1024


class ReportPreview:
    """Track JSON string paths while a structured completion is still arriving."""

    def __init__(self) -> None:
        self.stack: list[dict[str, Any]] = []
        self.in_string = False
        self.escaped = False
        self.raw = ""
        self.path: tuple[str, ...] = ()
        self.is_key = False
        self.values: dict[str, str] = {}

    def _value_path(self) -> tuple[str, ...]:
        if not self.stack:
            return ()
        top = self.stack[-1]
        return tuple(top["path"]) + (str(top.get("key") or "[]"),)

    def _decode(self) -> None:
        if self.is_key or self.path not in {("report", "conclusion"), ("report", "structure")}:
            return
        for trim in range(min(7, len(self.raw) + 1)):
            text = self.raw[:-trim] if trim else self.raw
            try:
                value = json.loads('"' + text + '"')
            except ValueError:
                continue
            # A high surrogate may arrive before its low surrogate in the next chunk.
            if value and 0xD800 <= ord(value[-1]) <= 0xDFFF:
                value = value[:-1]
            self.values[self.path[-1]] = value
            break

    def feed(self, fragment: str) -> dict[str, str]:
        for char in fragment:
            if self.in_string:
                if char == '"' and not self.escaped:
                    self.in_string = False
                    if self.is_key and self.stack:
                        try:
                            self.stack[-1]["key"] = json.loads('"' + self.raw + '"')
                            self.stack[-1]["key_expected"] = False
                        except ValueError:
                            pass
                    else:
                        self._decode()
                    continue
                self.raw += char
                self.escaped = not self.escaped if char == "\\" else False
                continue
            if char == '"':
                self.in_string = True
                self.raw = ""
                self.escaped = False
                self.is_key = bool(self.stack and self.stack[-1].get("key_expected"))
                self.path = self._value_path()
            elif char in "{[":
                self.stack.append(
                    {
                        "path": self._value_path(),
                        "key_expected": char == "{",
                        "object": char == "{",
                    }
                )
            elif char in "}]":
                if self.stack:
                    self.stack.pop()
            elif char == "," and self.stack and self.stack[-1]["object"]:
                self.stack[-1]["key_expected"] = True
                self.stack[-1]["key"] = None
        if self.in_string:
            self._decode()
        return dict(self.values)


class PreviewRelay:
    def __init__(self, send: Callable[[str, str, int], None]):
        self.preview = ReportPreview()
        self.send = send
        self.sent: dict[str, str] = {}
        self.last_flush = time.monotonic()

    def flush(self) -> None:
        for field, text in self.preview.values.items():
            old = self.sent.get(field, "")
            if text == old:
                continue
            offset = len(old) if text.startswith(old) else 0
            self.send(field, text[offset:], offset)
            self.sent[field] = text
        self.last_flush = time.monotonic()

    def feed(self, fragment: str) -> None:
        self.preview.feed(fragment)
        growth = sum(
            len(text) - len(self.sent.get(field, "")) for field, text in self.preview.values.items()
        )
        if not self.sent or growth >= 256 or time.monotonic() - self.last_flush >= 0.15:
            self.flush()


def read_completion(
    response: httpx.Response,
    on_content: Callable[[str], None],
    check_deadline: Callable[[], None],
) -> tuple[dict[str, Any], bytes]:
    if "text/event-stream" not in response.headers.get("content-type", ""):
        plain = response.read()
        if len(plain) > MAX_RESPONSE_BYTES:
            raise ValueError("Model response is too large")
        value = json.loads(plain)
        if not isinstance(value, dict):
            raise ValueError("Model response is not an object")
        return value, plain
    raw = bytearray()
    decoder = codecs.getincrementaldecoder("utf-8")()
    pending = ""
    data: list[str] = []
    content: list[str] = []
    model: str | None = None
    usage: dict[str, Any] | None = None
    finish: str | None = None
    done = False

    def frame() -> None:
        nonlocal model, usage, finish, done
        if not data:
            return
        payload = "\n".join(data)
        data.clear()
        if payload == "[DONE]":
            done = True
            return
        value = json.loads(payload)
        if not isinstance(value, dict):
            raise ValueError("Invalid stream frame")
        if value.get("model"):
            if model is not None and model != value["model"]:
                raise ValueError("Model changed during stream")
            model = value["model"]
        if isinstance(value.get("usage"), dict):
            usage = value["usage"]
        for choice in value.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            text = delta.get("content")
            if text:
                if not isinstance(text, str):
                    raise ValueError("Invalid content delta")
                content.append(text)
                on_content(text)
            if choice.get("finish_reason") is not None:
                finish = choice["finish_reason"]

    for chunk in response.iter_bytes():
        check_deadline()
        raw.extend(chunk)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError("Model stream exceeded its byte limit")
        pending += decoder.decode(chunk)
        while "\n" in pending:
            line, pending = pending.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                frame()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip(" "))
    pending += decoder.decode(b"", final=True)
    if pending.startswith("data:"):
        data.append(pending[5:].strip())
    frame()
    if not done or usage is None or finish is None:
        raise ValueError("Stream ended without a complete usage receipt")
    return {
        "model": model,
        "usage": usage,
        "choices": [{"finish_reason": finish, "message": {"content": "".join(content)}}],
    }, bytes(raw)
