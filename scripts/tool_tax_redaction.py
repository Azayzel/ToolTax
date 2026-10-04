#!/usr/bin/env python3
"""Share-safe redaction helpers for ToolTax traces and reports.

The redactor is intentionally conservative and deterministic within one process.
It removes common secrets/PII while preserving structure and aggregate metrics.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SENSITIVE_KEY_RE = re.compile(
    r"(?:^token$)|(?:^|[_-])(?:password|passwd|passphrase|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|"
    r"auth(?:orization)?|cookie|set[_-]?cookie|private[_-]?key|client[_-]?secret|credential|bearer)(?:$|[_-])",
    re.IGNORECASE,
)

PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.IGNORECASE)),
    ("openai-key", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")),
    ("slack-token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b")),
    ("aws-key", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")),
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}={0,2}")),
    ("basic-auth-url", re.compile(r"(?i)\b(https?://)[^\s/@:]+:[^\s/@]+@")),
    ("user-path-posix", re.compile(r"(?P<prefix>/(?:Users|home)/)[^/\s]+")),
    ("user-path-windows", re.compile(r"(?i)(?P<prefix>[A-Z]:\\Users\\)[^\\\s]+")),
)


@dataclass
class Redactor:
    """Redact common secrets and identity-bearing strings.

    Placeholders are stable only within this Redactor instance. This preserves
    repeated-value relationships without making the output a reusable hash oracle.
    """

    _ids: dict[tuple[str, str], int] = field(default_factory=dict)
    _next_id: int = 1

    def placeholder(self, kind: str, value: Any) -> str:
        raw = str(value)
        key = (kind, raw)
        if key not in self._ids:
            self._ids[key] = self._next_id
            self._next_id += 1
        return f"<redacted:{kind}:{self._ids[key]}>"

    def redact_string(self, value: str) -> str:
        text = value
        for kind, pattern in PATTERNS:
            if kind == "basic-auth-url":
                def url_repl(match: re.Match[str]) -> str:
                    return match.group(1) + self.placeholder(kind, match.group(0)) + "@"
                text = pattern.sub(url_repl, text)
            elif kind in {"user-path-posix", "user-path-windows"}:
                def path_repl(match: re.Match[str], _kind: str = kind) -> str:
                    return match.group("prefix") + self.placeholder(_kind, match.group(0))
                text = pattern.sub(path_repl, text)
            else:
                text = pattern.sub(lambda m, _kind=kind: self.placeholder(_kind, m.group(0)), text)
        return text

    def redact(self, value: Any, key_name: str | None = None) -> Any:
        if key_name and SENSITIVE_KEY_RE.search(key_name):
            if value in (None, "", [], {}):
                return value
            return self.placeholder("secret", value)
        if isinstance(value, str):
            return self.redact_string(value)
        if isinstance(value, list):
            return [self.redact(item) for item in value]
        if isinstance(value, tuple):
            return [self.redact(item) for item in value]
        if isinstance(value, dict):
            return {str(k): self.redact(v, str(k)) for k, v in value.items()}
        return value
