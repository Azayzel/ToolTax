#!/usr/bin/env python3
"""ToolTax: measure tool-call waste in agent session traces.

Dependency-free on purpose. Supports:
- canonical ToolTax JSONL
- Claude Code JSONL transcripts (best effort; schema is not stable/public)
- OpenAI Codex rollout JSONL (best effort)

This tool reports *observed payload tokens* using a deterministic chars/4 estimate.
It does not claim to reconstruct provider billing or hidden prompt/tool-schema cost.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import statistics
import sys
from collections import Counter, defaultdict
from typing import Any, Iterable, Iterator

VERSION = "0.3.0"

ERROR_HINTS = (
    "error", "failed", "failure", "exception", "traceback", "not found",
    "permission denied", "timed out", "timeout", "invalid", "unauthorized",
)
EMPTY_HINTS = ("", "null", "none", "[]", "{}")
MCP_PATTERNS = (
    re.compile(r"^mcp__([^_][^_]*)__+(.+)$"),
    re.compile(r"^mcp[_-]([^:_/-]+)[_:/-](.+)$"),
)


def token_estimate(value: Any) -> int:
    """Deterministic, provider-neutral rough token estimate."""
    if value is None:
        return 0
    if not isinstance(value, str):
        try:
            value = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        except Exception:
            value = str(value)
    return max(0, math.ceil(len(value) / 4))


def parse_ts(value: Any) -> dt.datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = dt.datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed
    except ValueError:
        return None


def stable_json(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        return str(value)


def short_hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8", errors="replace")).hexdigest()[:12]


def flatten_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return "\n".join(flatten_text(v) for v in value)
    if isinstance(value, dict):
        # Common tool result payloads first.
        for key in ("content", "text", "output", "stdout", "stderr", "result", "message"):
            if key in value:
                text = flatten_text(value[key])
                if text:
                    return text
        return stable_json(value)
    return str(value)


def tool_identity(name: str, namespace: str | None = None) -> tuple[str, str]:
    raw = name or "unknown"
    ns = (namespace or "").strip()
    if ns:
        clean = ns.strip("_:/-")
        if clean.startswith("mcp__"):
            clean = clean[5:].strip("_:/-")
        if clean:
            return clean, raw
    for pattern in MCP_PATTERNS:
        match = pattern.match(raw)
        if match:
            return match.group(1), match.group(2)
    return "builtin", raw


@dataclasses.dataclass
class ToolCall:
    source: str
    session: str
    call_id: str
    tool: str
    server: str
    started_at: dt.datetime | None = None
    ended_at: dt.datetime | None = None
    arguments: Any = None
    result: Any = None
    error: bool = False
    explicit_success: bool | None = None
    model_input_tokens: int = 0
    model_output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    source_file: str = ""
    captured_input_tokens: int | None = None
    captured_output_tokens: int | None = None
    captured_latency_ms: int | None = None

    @property
    def input_tokens(self) -> int:
        return self.captured_input_tokens if self.captured_input_tokens is not None else token_estimate(self.arguments)

    @property
    def output_tokens(self) -> int:
        return self.captured_output_tokens if self.captured_output_tokens is not None else token_estimate(self.result)

    @property
    def observed_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def latency_ms(self) -> int | None:
        if self.captured_latency_ms is not None:
            return self.captured_latency_ms
        if not self.started_at or not self.ended_at:
            return None
        return max(0, int((self.ended_at - self.started_at).total_seconds() * 1000))

    @property
    def empty_result(self) -> bool:
        return flatten_text(self.result).strip().lower() in EMPTY_HINTS

    @property
    def signature(self) -> str:
        return f"{self.server}:{self.tool}:{short_hash(self.arguments)}"


@dataclasses.dataclass
class ParseStats:
    files: int = 0
    lines: int = 0
    malformed_lines: int = 0
    source_counts: Counter = dataclasses.field(default_factory=Counter)
    model_input_tokens: int = 0
    model_output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    advertised_schema_tokens: int = 0
    advertised_schema_bytes: int = 0
    advertised_tool_count: int = 0
    schema_files: int = 0
    protocol_request_bytes: int = 0
    protocol_response_bytes: int = 0


def iter_jsonl(path: pathlib.Path, stats: ParseStats) -> Iterator[dict[str, Any]]:
    stats.files += 1
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            stats.lines += 1
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                stats.malformed_lines += 1
                continue
            if isinstance(value, dict):
                yield value


def sniff_source(record: dict[str, Any]) -> str:
    if record.get("schema") == "tooltax.v1" or record.get("event") in {"tool_call", "tool_result"}:
        return "canonical"
    rtype = record.get("type")
    if rtype in {"assistant", "user", "tool_result", "tool_error", "queue-operation", "mcp_instructions_delta", "deferred_tools_delta"}:
        return "claude"
    if rtype in {"response_item", "event_msg", "session_meta", "turn_context", "compacted"}:
        return "codex"
    payload = record.get("payload")
    if isinstance(payload, dict) and payload.get("type") in {"function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"}:
        return "codex"
    return "unknown"


def parse_canonical(records: Iterable[dict[str, Any]], session: str, source_file: str, stats: ParseStats) -> list[ToolCall]:
    pending: dict[str, ToolCall] = {}
    out: list[ToolCall] = []
    final_schema_tokens = 0
    final_schema_bytes = 0
    final_tool_count = 0
    final_request_bytes = 0
    final_response_bytes = 0
    saw_schema = False
    for rec in records:
        event = rec.get("event") or rec.get("type")
        cid = str(rec.get("call_id") or rec.get("id") or "")
        if event == "tool_schema_snapshot":
            saw_schema = True
            final_schema_tokens = int(rec.get("estimated_schema_tokens") or rec.get("schema_tokens") or final_schema_tokens or 0)
            final_schema_bytes = int(rec.get("schema_bytes") or final_schema_bytes or 0)
            final_tool_count = int(rec.get("tool_count") or final_tool_count or 0)
            continue
        if event == "capture_end":
            if rec.get("schema_tokens") is not None:
                saw_schema = True
                final_schema_tokens = int(rec.get("schema_tokens") or 0)
                final_schema_bytes = int(rec.get("schema_bytes") or 0)
            final_request_bytes = int(rec.get("request_bytes") or final_request_bytes or 0)
            final_response_bytes = int(rec.get("response_bytes") or final_response_bytes or 0)
            continue
        if event == "tool_call":
            server, tool = tool_identity(str(rec.get("tool") or rec.get("name") or "unknown"), rec.get("server"))
            call = ToolCall(
                source="canonical", session=str(rec.get("session") or session), call_id=cid or short_hash(rec),
                tool=tool, server=server, started_at=parse_ts(rec.get("timestamp")),
                arguments=rec.get("arguments", rec.get("input")), source_file=source_file,
                captured_input_tokens=int(rec["estimated_input_tokens"]) if rec.get("estimated_input_tokens") is not None else None,
            )
            pending[call.call_id] = call
            out.append(call)
        elif event == "tool_result":
            call = pending.get(cid)
            if not call:
                server, tool = tool_identity(str(rec.get("tool") or "unknown"), rec.get("server"))
                call = ToolCall(source="canonical", session=str(rec.get("session") or session), call_id=cid or short_hash(rec), tool=tool, server=server, source_file=source_file)
                out.append(call)
            call.ended_at = parse_ts(rec.get("timestamp"))
            call.result = rec.get("result", rec.get("output"))
            call.error = bool(rec.get("error", False))
            if rec.get("estimated_output_tokens") is not None:
                call.captured_output_tokens = int(rec.get("estimated_output_tokens") or 0)
            if rec.get("latency_ms") is not None:
                call.captured_latency_ms = int(rec.get("latency_ms") or 0)
            if "success" in rec:
                call.explicit_success = bool(rec["success"])
    if saw_schema:
        stats.schema_files += 1
        stats.advertised_schema_tokens += final_schema_tokens
        stats.advertised_schema_bytes += final_schema_bytes
        stats.advertised_tool_count += final_tool_count
    stats.protocol_request_bytes += final_request_bytes
    stats.protocol_response_bytes += final_response_bytes
    return out


def claude_content_blocks(rec: dict[str, Any]) -> list[dict[str, Any]]:
    message = rec.get("message")
    if isinstance(message, dict):
        content = message.get("content")
    else:
        content = rec.get("content")
    if isinstance(content, dict):
        return [content]
    return [b for b in (content or []) if isinstance(b, dict)] if isinstance(content, list) else []


def parse_claude(records: Iterable[dict[str, Any]], session: str, source_file: str, stats: ParseStats) -> list[ToolCall]:
    pending: dict[str, ToolCall] = {}
    out: list[ToolCall] = []
    for rec in records:
        timestamp = parse_ts(rec.get("timestamp"))
        rtype = rec.get("type")
        sid = str(rec.get("sessionId") or rec.get("session_id") or session)
        message = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        usage = message.get("usage") if isinstance(message.get("usage"), dict) else {}
        if rtype == "assistant" and usage:
            stats.model_input_tokens += int(usage.get("input_tokens") or 0)
            stats.model_output_tokens += int(usage.get("output_tokens") or 0)
            stats.cache_read_tokens += int(usage.get("cache_read_input_tokens") or 0)
            stats.cache_write_tokens += int(usage.get("cache_creation_input_tokens") or 0)
        for block in claude_content_blocks(rec):
            btype = block.get("type")
            if btype == "tool_use":
                cid = str(block.get("id") or short_hash(block))
                raw_name = str(block.get("name") or "unknown")
                server, tool = tool_identity(raw_name)
                call = ToolCall(
                    source="claude", session=sid, call_id=cid, tool=tool, server=server,
                    started_at=timestamp, arguments=block.get("input"), source_file=source_file,
                    model_input_tokens=int(usage.get("input_tokens") or 0),
                    model_output_tokens=int(usage.get("output_tokens") or 0),
                    cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                    cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0),
                )
                pending[cid] = call
                out.append(call)
            elif btype == "tool_result":
                cid = str(block.get("tool_use_id") or block.get("id") or "")
                call = pending.get(cid)
                if call:
                    call.ended_at = timestamp
                    call.result = block.get("content")
                    call.error = bool(block.get("is_error", False))
        # Some third-party/exported transcripts store tool results top-level.
        if rtype in {"tool_result", "tool_error"}:
            cid = str(rec.get("tool_use_id") or rec.get("call_id") or rec.get("id") or "")
            call = pending.get(cid)
            if call:
                call.ended_at = timestamp
                call.result = rec.get("content", rec.get("result"))
                call.error = rtype == "tool_error" or bool(rec.get("is_error", False))
    return out


def parse_arguments(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def parse_codex(records: Iterable[dict[str, Any]], session: str, source_file: str, stats: ParseStats) -> list[ToolCall]:
    pending: dict[str, ToolCall] = {}
    out: list[ToolCall] = []
    for rec in records:
        timestamp = parse_ts(rec.get("timestamp"))
        rtype = rec.get("type")
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else rec
        ptype = payload.get("type")
        if rtype == "session_meta":
            session = str(payload.get("session_id") or payload.get("id") or session)
        # Token count events vary by version; accumulate last/cumulative values only for context display.
        if rtype == "event_msg" and ptype == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else payload
            last = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else info.get("token_usage")
            if isinstance(last, dict):
                stats.model_input_tokens += int(last.get("input_tokens") or last.get("input") or 0)
                stats.model_output_tokens += int(last.get("output_tokens") or last.get("output") or 0)
                stats.cache_read_tokens += int(last.get("cached_input_tokens") or last.get("cached") or 0)
        if ptype in {"function_call", "custom_tool_call"}:
            cid = str(payload.get("call_id") or payload.get("id") or short_hash(payload))
            raw_name = str(payload.get("name") or payload.get("tool") or "unknown")
            server, tool = tool_identity(raw_name, payload.get("namespace"))
            args = payload.get("arguments", payload.get("input"))
            call = ToolCall(
                source="codex", session=session, call_id=cid, tool=tool, server=server,
                started_at=timestamp, arguments=parse_arguments(args), source_file=source_file,
            )
            pending[cid] = call
            out.append(call)
        elif ptype in {"function_call_output", "custom_tool_call_output"}:
            cid = str(payload.get("call_id") or payload.get("id") or "")
            call = pending.get(cid)
            if not call and payload.get("name"):
                # Newer async delegation outputs can omit call_id. Keep them visible but unmatched.
                server, tool = tool_identity(str(payload.get("name")), payload.get("namespace"))
                call = ToolCall(source="codex", session=session, call_id=cid or short_hash(payload), tool=tool, server=server, source_file=source_file)
                out.append(call)
            if call:
                call.ended_at = timestamp
                call.result = payload.get("output", payload.get("result"))
                text = flatten_text(call.result).lower()
                call.error = any(h in text for h in ERROR_HINTS)
    return out


def load_file(path: pathlib.Path, forced_source: str, stats: ParseStats) -> list[ToolCall]:
    records = list(iter_jsonl(path, stats))
    if not records:
        return []
    source = forced_source if forced_source != "auto" else next((sniff_source(r) for r in records if sniff_source(r) != "unknown"), "unknown")
    stats.source_counts[source] += 1
    session = path.stem
    if source == "canonical":
        return parse_canonical(records, session, str(path), stats)
    if source == "claude":
        return parse_claude(records, session, str(path), stats)
    if source == "codex":
        return parse_codex(records, session, str(path), stats)
    return []


def discover_paths(inputs: list[str], source: str) -> list[pathlib.Path]:
    paths: list[pathlib.Path] = []
    if not inputs:
        home = pathlib.Path.home()
        if source in {"auto", "claude"}:
            root = home / ".claude" / "projects"
            if root.exists():
                paths.extend(root.rglob("*.jsonl"))
        if source in {"auto", "codex"}:
            root = home / ".codex" / "sessions"
            if root.exists():
                paths.extend(root.rglob("*.jsonl"))
    else:
        for raw in inputs:
            path = pathlib.Path(os.path.expanduser(raw))
            if path.is_dir():
                paths.extend(path.rglob("*.jsonl"))
            elif path.is_file():
                paths.append(path)
    return sorted(set(p.resolve() for p in paths), key=str)


@dataclasses.dataclass
class Finding:
    kind: str
    severity: str
    call: ToolCall
    waste_tokens: int
    detail: str


def analyze(calls: list[ToolCall], duplicate_window: int = 8, large_output_tokens: int = 2000) -> list[Finding]:
    findings: list[Finding] = []
    recent: list[str] = []
    for call in calls:
        text = flatten_text(call.result).strip().lower()
        is_error = call.error or call.explicit_success is False
        if is_error:
            findings.append(Finding("error", "high", call, call.observed_tokens, "tool call failed"))
        if call.empty_result and call.result is not None and not is_error:
            findings.append(Finding("empty-result", "medium", call, call.observed_tokens, "tool returned no useful payload"))
        if call.output_tokens >= large_output_tokens:
            excess = call.output_tokens - large_output_tokens
            findings.append(Finding("output-bloat", "medium", call, excess, f"output ~{call.output_tokens:,} tokens"))
        if call.signature in recent:
            # Do not double-count all payload: the output may have changed. Count repeated input as definite duplication.
            findings.append(Finding("duplicate-call", "medium", call, call.input_tokens, f"same tool + arguments repeated within {duplicate_window} calls"))
        recent.append(call.signature)
        if len(recent) > duplicate_window:
            recent.pop(0)
        if call.result is None:
            findings.append(Finding("unmatched-call", "low", call, call.input_tokens, "no matching tool result found in trace"))
        elif not is_error and any(h in text for h in ("rate limit", "try again", "temporarily unavailable")):
            findings.append(Finding("retry-signal", "medium", call, call.observed_tokens, "result suggests a retry/recoverable failure"))
    return findings


def group_metrics(calls: list[ToolCall], findings: list[Finding]) -> list[dict[str, Any]]:
    by_server: dict[str, list[ToolCall]] = defaultdict(list)
    waste_by_server: Counter = Counter()
    kinds_by_server: dict[str, Counter] = defaultdict(Counter)
    for call in calls:
        by_server[call.server].append(call)
    for finding in findings:
        waste_by_server[finding.call.server] += finding.waste_tokens
        kinds_by_server[finding.call.server][finding.kind] += 1

    rows = []
    for server, items in by_server.items():
        total = sum(c.observed_tokens for c in items)
        waste = min(total, waste_by_server[server]) if total else waste_by_server[server]
        successes = sum(1 for c in items if not c.error and c.explicit_success is not False and c.result is not None)
        errors = sum(1 for c in items if c.error or c.explicit_success is False)
        latencies = [c.latency_ms for c in items if c.latency_ms is not None]
        utility = 100.0
        if items:
            utility -= 35 * (errors / len(items))
            utility -= 40 * (waste / max(1, total))
            utility -= 10 * (kinds_by_server[server]["duplicate-call"] / len(items))
            utility -= 15 * (kinds_by_server[server]["empty-result"] / len(items))
        utility = max(0.0, min(100.0, utility))
        rows.append({
            "server": server,
            "calls": len(items),
            "successes": successes,
            "errors": errors,
            "input_tokens": sum(c.input_tokens for c in items),
            "output_tokens": sum(c.output_tokens for c in items),
            "observed_tokens": total,
            "waste_tokens": waste,
            "waste_pct": (100 * waste / total) if total else 0.0,
            "utility_score": utility,
            "median_latency_ms": int(statistics.median(latencies)) if latencies else None,
            "findings": dict(kinds_by_server[server]),
            "tools": sorted(Counter(c.tool for c in items).items(), key=lambda kv: (-kv[1], kv[0])),
        })
    return sorted(rows, key=lambda r: (-r["waste_tokens"], -r["observed_tokens"], r["server"]))


def recommendation(row: dict[str, Any]) -> str:
    calls = row["calls"]
    waste = row["waste_pct"]
    score = row["utility_score"]
    errors = row["errors"]
    findings = row["findings"]
    if calls >= 5 and score < 45 and waste >= 35:
        return "consider disabling or replacing"
    if findings.get("duplicate-call", 0) >= max(2, calls // 4):
        return "deduplicate/reuse results"
    if findings.get("output-bloat", 0) >= 2:
        return "cap or filter tool output"
    if errors >= max(2, calls // 3):
        return "investigate reliability"
    if score >= 85 and waste < 20:
        return "keep"
    return "monitor"


def report_data(
    calls: list[ToolCall],
    findings: list[Finding],
    stats: ParseStats,
    files: list[pathlib.Path],
    share_safe: bool = False,
) -> dict[str, Any]:
    rows = group_metrics(calls, findings)
    total = sum(c.observed_tokens for c in calls)
    waste = min(total, sum(f.waste_tokens for f in findings)) if total else sum(f.waste_tokens for f in findings)
    latencies = [c.latency_ms for c in calls if c.latency_ms is not None]
    errors = sum(1 for c in calls if c.error or c.explicit_success is False)
    duplicate_calls = sum(1 for f in findings if f.kind == "duplicate-call")
    return {
        "version": VERSION,
        "files_scanned": len(files),
        "calls": len(calls),
        "errors": errors,
        "duplicate_calls": duplicate_calls,
        "observed_tokens": total,
        "estimated_waste_tokens": waste,
        "estimated_waste_pct": (100 * waste / total) if total else 0.0,
        "median_latency_ms": int(statistics.median(latencies)) if latencies else None,
        "advertised_schema_tokens": stats.advertised_schema_tokens,
        "advertised_schema_bytes": stats.advertised_schema_bytes,
        "advertised_tool_count": stats.advertised_tool_count,
        "schema_files": stats.schema_files,
        "protocol_request_bytes": stats.protocol_request_bytes,
        "protocol_response_bytes": stats.protocol_response_bytes,
        "malformed_lines": stats.malformed_lines,
        "sources": dict(stats.source_counts),
        "model_usage_observed": {
            "input_tokens": stats.model_input_tokens,
            "output_tokens": stats.model_output_tokens,
            "cache_read_tokens": stats.cache_read_tokens,
            "cache_write_tokens": stats.cache_write_tokens,
        },
        "servers": [{**row, "recommendation": recommendation(row)} for row in rows],
        "findings": [
            {
                "kind": f.kind, "severity": f.severity, "server": f.call.server, "tool": f.call.tool,
                "call_id": f.call.call_id, "waste_tokens": f.waste_tokens, "detail": f.detail,
                "source_file": "<redacted:path>" if share_safe else f.call.source_file,
            }
            for f in findings
        ],
        "share_safe": share_safe,
        "notes": [
            "Tool payload token counts are provider-neutral estimates unless a live capture supplied preserved estimates; they are not provider billing.",
            "Advertised schema tokens are measured from MCP tools/list definitions and are not guaranteed to equal provider/model context cost.",
            "Claude end-to-end latency may include time waiting for user approval.",
            "Utility score is a heuristic based on failures, duplication, empty results, and payload waste; it is not causal ROI.",
        ],
    }


def pct_change(before: int | float | None, after: int | float | None) -> float | None:
    if before is None or after is None or before == 0:
        return None
    return 100.0 * (after - before) / before


def comparison_data(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    metric_specs = [
        ("advertised_schema_tokens", "Advertised schema", "tok", True),
        ("observed_tokens", "Observed tool payload", "tok", True),
        ("estimated_waste_tokens", "Estimated waste", "tok", True),
        ("calls", "Tool calls", "calls", True),
        ("errors", "Errors", "errors", True),
        ("duplicate_calls", "Duplicate calls", "calls", True),
        ("median_latency_ms", "Median latency", "ms", True),
    ]
    metrics = []
    for key, label, unit, lower_is_better in metric_specs:
        b = before.get(key)
        a = after.get(key)
        delta = None if b is None or a is None else a - b
        change = pct_change(b, a)
        metrics.append({
            "key": key, "label": label, "unit": unit, "before": b, "after": a,
            "delta": delta, "change_pct": change, "lower_is_better": lower_is_better,
        })

    before_servers = {row["server"]: row for row in before.get("servers", [])}
    after_servers = {row["server"]: row for row in after.get("servers", [])}
    servers = []
    for server in sorted(set(before_servers) | set(after_servers)):
        b = before_servers.get(server, {})
        a = after_servers.get(server, {})
        servers.append({
            "server": server,
            "before_calls": b.get("calls", 0),
            "after_calls": a.get("calls", 0),
            "before_tokens": b.get("observed_tokens", 0),
            "after_tokens": a.get("observed_tokens", 0),
            "before_waste_tokens": b.get("waste_tokens", 0),
            "after_waste_tokens": a.get("waste_tokens", 0),
            "before_utility": b.get("utility_score"),
            "after_utility": a.get("utility_score"),
        })
    waste_before = before.get("estimated_waste_tokens") or 0
    waste_after = after.get("estimated_waste_tokens") or 0
    waste_reduction_pct = None if not waste_before else 100.0 * (waste_before - waste_after) / waste_before
    return {
        "version": VERSION,
        "mode": "before-after",
        "before": before,
        "after": after,
        "metrics": metrics,
        "servers": servers,
        "estimated_waste_reduction_pct": waste_reduction_pct,
    }


def fmt_int(value: int | float | None) -> str:
    if value is None:
        return "—"
    return f"{int(value):,}"


def print_text(data: dict[str, Any], limit: int = 20) -> None:
    print("TOOLTAX — AGENT TOOL COST REPORT")
    print("=" * 68)
    print(f"Calls: {data['calls']:,}   Observed payload: ~{data['observed_tokens']:,} tok   Estimated waste: ~{data['estimated_waste_tokens']:,} tok ({data['estimated_waste_pct']:.1f}%)")
    print(f"Files: {data['files_scanned']:,}   Sources: {', '.join(f'{k}:{v}' for k,v in data['sources'].items()) or 'none'}")
    if data.get("schema_files"):
        print(f"Advertised schema: ~{data['advertised_schema_tokens']:,} tok across {data['schema_files']} capture file(s)   Median latency: {fmt_int(data.get('median_latency_ms'))} ms")
    print()
    if not data["servers"]:
        print("No supported tool calls found.")
        return
    print(f"{'SERVER':<20} {'CALLS':>6} {'TOKENS':>10} {'WASTE':>8} {'SCORE':>7}  RECOMMENDATION")
    print("-" * 82)
    for row in data["servers"][:limit]:
        print(f"{row['server'][:20]:<20} {row['calls']:>6} {row['observed_tokens']:>10,} {row['waste_pct']:>7.1f}% {row['utility_score']:>6.0f}/100  {row['recommendation']}")
    print()
    top = sorted(data["findings"], key=lambda f: (-f["waste_tokens"], f["server"], f["tool"]))[:8]
    if top:
        print("TOP WASTE SIGNALS")
        for f in top:
            print(f"- {f['server']}/{f['tool']}: {f['kind']} (~{f['waste_tokens']:,} tok) — {f['detail']}")
        print()
    print("Interpretation: observed payload cost only; provider prompt/schema/billing cost is not reconstructed.")


def print_markdown(data: dict[str, Any]) -> None:
    print("# ToolTax report\n")
    print(f"**{data['calls']:,} calls** × **~{data['observed_tokens']:,} observed payload tokens** × **~{data['estimated_waste_tokens']:,} estimated waste ({data['estimated_waste_pct']:.1f}%)**\n")
    print("| Server | Calls | Observed tokens | Waste | Utility | Recommendation |")
    print("|---|---:|---:|---:|---:|---|")
    for row in data["servers"]:
        print(f"| `{row['server']}` | {row['calls']} | {row['observed_tokens']:,} | {row['waste_pct']:.1f}% | {row['utility_score']:.0f}/100 | {row['recommendation']} |")
    print("\n## Highest-cost waste signals\n")
    for f in sorted(data["findings"], key=lambda x: -x["waste_tokens"])[:10]:
        print(f"- **{f['server']}/{f['tool']}** — {f['kind']}: ~{f['waste_tokens']:,} tokens ({f['detail']})")
    print("\n> Tool token counts are payload estimates, not provider billing totals. Utility is a heuristic, not causal ROI.")



def print_comparison_text(data: dict[str, Any]) -> None:
    print("TOOLTAX — BEFORE / AFTER")
    print("=" * 78)
    print(f"{'METRIC':<26} {'BEFORE':>14} {'AFTER':>14} {'CHANGE':>16}")
    print("-" * 78)
    for row in data["metrics"]:
        before = fmt_int(row["before"])
        after = fmt_int(row["after"])
        change = "—" if row["change_pct"] is None else f"{row['change_pct']:+.1f}%"
        unit = row["unit"]
        if row["before"] is not None:
            before = f"{before} {unit}"
        if row["after"] is not None:
            after = f"{after} {unit}"
        print(f"{row['label']:<26} {before:>14} {after:>14} {change:>16}")
    reduction = data.get("estimated_waste_reduction_pct")
    print()
    if reduction is None:
        print("Estimated waste reduction: — (baseline waste is zero or unavailable)")
    else:
        print(f"Estimated waste reduction: {reduction:.1f}%")
    changed = [r for r in data.get("servers", []) if r["before_tokens"] != r["after_tokens"] or r["before_waste_tokens"] != r["after_waste_tokens"]]
    if changed:
        print("\nSERVER CHANGES")
        print(f"{'SERVER':<22} {'CALLS':>11} {'PAYLOAD TOK':>23} {'WASTE TOK':>23}")
        print("-" * 82)
        for row in changed[:15]:
            calls_delta = f"{row['before_calls']}→{row['after_calls']}"
            payload_delta = f"{row['before_tokens']:,}→{row['after_tokens']:,}"
            waste_delta = f"{row['before_waste_tokens']:,}→{row['after_waste_tokens']:,}"
            print(f"{row['server'][:22]:<22} {calls_delta:>11} {payload_delta:>23} {waste_delta:>23}")
    print("\nInterpretation: lower is better for the displayed metrics; schema is advertised MCP definition size, not provider billing/context truth.")


def print_comparison_markdown(data: dict[str, Any]) -> None:
    print("# ToolTax before / after\n")
    print("| Metric | Before | After | Change |")
    print("|---|---:|---:|---:|")
    for row in data["metrics"]:
        b = "—" if row["before"] is None else f"{fmt_int(row['before'])} {row['unit']}"
        a = "—" if row["after"] is None else f"{fmt_int(row['after'])} {row['unit']}"
        c = "—" if row["change_pct"] is None else f"{row['change_pct']:+.1f}%"
        print(f"| {row['label']} | {b} | {a} | {c} |")
    reduction = data.get("estimated_waste_reduction_pct")
    if reduction is not None:
        print(f"\n**Estimated waste reduction: {reduction:.1f}%**\n")
    print("> Lower is better for these metrics. Advertised schema size is measured from MCP `tools/list`, not reconstructed provider billing or exact model-context cost.")


def analyze_inputs(inputs: list[str], args: argparse.Namespace) -> dict[str, Any]:
    stats = ParseStats()
    files = discover_paths(inputs, args.source)
    calls: list[ToolCall] = []
    for path in files:
        try:
            calls.extend(load_file(path, args.source, stats))
        except (OSError, PermissionError) as exc:
            print(f"warning: {path}: {exc}", file=sys.stderr)
    findings = analyze(calls, args.duplicate_window, args.large_output_tokens)
    return report_data(calls, findings, stats, files, share_safe=args.share_safe)

def demo_calls() -> list[ToolCall]:
    now = dt.datetime.now(dt.timezone.utc)
    def c(i: int, server: str, tool: str, args: Any, result: Any, error: bool = False) -> ToolCall:
        return ToolCall("demo", "demo", f"demo-{i}", tool, server, now, now + dt.timedelta(milliseconds=80+i*7), args, result, error)
    huge = "result " * 10000
    return [
        c(1, "github", "search_code", {"q":"TODO repo:acme/app"}, {"items":[1,2,3]}),
        c(2, "github", "get_file", {"path":"README.md"}, "# App\nQuickstart..."),
        c(3, "memory", "search", {"q":"project context"}, []),
        c(4, "memory", "search", {"q":"project context"}, []),
        c(5, "context7", "docs", {"lib":"react","topic":"cache"}, huge),
        c(6, "filesystem", "read", {"path":"missing.txt"}, "Error: file not found", True),
        c(7, "github", "search_code", {"q":"TODO repo:acme/app"}, {"items":[1,2,3]}),
        c(8, "context7", "docs", {"lib":"react","topic":"actions"}, huge),
        c(9, "memory", "get", {"key":"preferences"}, None),
        c(10, "github", "list_prs", {"state":"open"}, {"prs":[1,2]}),
    ]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tool-tax", description="Find token/resource waste in AI agent tool usage.")
    parser.add_argument("paths", nargs="*", help="JSONL file(s) or directories. With no paths, scans known local Claude/Codex session locations.")
    parser.add_argument("--source", choices=["auto", "claude", "codex", "canonical"], default="auto", help="Force input adapter (default: auto).")
    parser.add_argument("--format", choices=["text", "json", "markdown"], default="text", help="Output format.")
    parser.add_argument("--output", "-o", help="Write report to a file instead of stdout.")
    parser.add_argument("--duplicate-window", type=int, default=8, help="Calls within which an identical tool+argument call is considered duplicate.")
    parser.add_argument("--large-output-tokens", type=int, default=2000, help="Estimated output token threshold for bloat signal.")
    parser.add_argument("--demo", action="store_true", help="Show a deterministic demo report.")
    parser.add_argument("--before", action="append", default=[], metavar="PATH", help="Baseline trace/file/directory for before/after comparison; repeatable.")
    parser.add_argument("--after", action="append", default=[], metavar="PATH", help="Optimized trace/file/directory for before/after comparison; repeatable.")
    parser.add_argument("--share-safe", action="store_true", help="Suppress local source paths in report output. Use scripts/tool_tax_redact.py to sanitize raw traces before sharing them.")
    parser.add_argument("--version", action="version", version=f"ToolTax {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    compare_mode = bool(args.before or args.after)
    if compare_mode and (not args.before or not args.after):
        raise SystemExit("before/after comparison requires at least one --before and one --after path")
    if compare_mode and (args.paths or args.demo):
        raise SystemExit("do not combine positional paths/--demo with --before/--after")

    if compare_mode:
        data = comparison_data(analyze_inputs(args.before, args), analyze_inputs(args.after, args))
    elif args.demo:
        stats = ParseStats()
        calls = demo_calls()
        findings = analyze(calls, args.duplicate_window, args.large_output_tokens)
        data = report_data(calls, findings, stats, [], share_safe=args.share_safe)
    else:
        data = analyze_inputs(args.paths, args)

    def emit() -> None:
        if args.format == "json":
            json.dump(data, sys.stdout, indent=2)
            print()
        elif args.format == "markdown":
            print_comparison_markdown(data) if compare_mode else print_markdown(data)
        else:
            print_comparison_text(data) if compare_mode else print_text(data)

    if args.output:
        target = pathlib.Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        old = sys.stdout
        with target.open("w", encoding="utf-8") as handle:
            sys.stdout = handle
            try:
                emit()
            finally:
                sys.stdout = old
        print(str(target))
    else:
        emit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
