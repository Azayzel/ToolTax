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
import itertools
import json
import math
import os
import pathlib
import re
import shutil
import statistics
import sys
import textwrap
import threading
import time
from collections import Counter, defaultdict
from typing import Any, Iterable, Iterator

VERSION = "0.3.0"

CLIENT_REGISTRY = {
    "claude": {"name": "Claude Code / Claude VS Code", "home": ".claude", "env": "CLAUDE_CONFIG_DIR"},
    "codex": {"name": "Codex", "home": ".codex", "env": "CODEX_HOME"},
    "cursor": {"name": "Cursor", "home": ".cursor"},
    "copilot": {"name": "VS Code / Copilot", "home": ".copilot"},
    "continue": {"name": "Continue", "home": ".continue"},
    "gemini": {"name": "Gemini CLI", "home": ".gemini"},
    "windsurf": {"name": "Windsurf", "home": ".codeium/windsurf"},
    "cline": {"name": "Cline", "home": ".cline"},
    "roo": {"name": "Roo Code", "home": ".roo"},
    "kiro": {"name": "Kiro", "home": ".kiro"},
    "opencode": {"name": "OpenCode", "config": "opencode"},
    "amp": {"name": "Amp", "config": "amp"},
    "goose": {"name": "Goose", "config": "goose"},
    "other": {"name": "Other detected MCP clients"},
}

ERROR_HINTS = (
    "error", "failed", "failure", "exception", "traceback", "not found",
    "permission denied", "timed out", "timeout", "invalid", "unauthorized",
)
EMPTY_HINTS = ("", "null", "none", "[]", "{}")
MCP_PATTERNS = (
    re.compile(r"^mcp__([^_][^_]*)__+(.+)$"),
    re.compile(r"^mcp[_-]([^:_/-]+)[_:/-](.+)$"),
)


def terminal_color(text: str, code: str, enabled: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if enabled else text


class TerminalProgress:
    def __init__(self, enabled: bool = False, color: bool = False) -> None:
        self.enabled = enabled
        self.color = color
        self.message = "Preparing local audit"
        self.stream = sys.stderr
        self.started = time.monotonic()
        self.stopped = threading.Event()
        self.worker: threading.Thread | None = None

    def update(self, message: str) -> None:
        self.message = message

    def draw(self, frame: int) -> None:
        width = max(1, shutil.get_terminal_size((80, 24)).columns - 1)
        position = frame % 10
        position = position if position < 6 else 10 - position
        scanner = "[" + " " * position + "=" + " " * (5 - position) + "]"
        elapsed = time.monotonic() - self.started
        line = f"  {scanner} {self.message}  {elapsed:.1f}s"[:width]
        self.stream.write("\r\033[2K" + terminal_color(line, "36", self.color))
        self.stream.flush()

    def animate(self) -> None:
        frame = 1
        while not self.stopped.wait(0.09):
            self.draw(frame)
            frame += 1

    def __enter__(self) -> TerminalProgress:
        if self.enabled:
            self.draw(0)
            self.worker = threading.Thread(target=self.animate, daemon=True)
            self.worker.start()
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.stopped.set()
        if self.worker is not None:
            self.worker.join()
        if self.enabled:
            self.stream.write("\r\033[2K")
            self.stream.flush()


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
    model: str = "unknown"
    client: str = ""
    media_parts: list[dict[str, Any]] = dataclasses.field(default_factory=list)

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
        return not self.media_parts and flatten_text(self.result).strip().lower() in EMPTY_HINTS

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
    copilot_sessions: Counter = dataclasses.field(default_factory=Counter)


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
    if record.get("kind") == 0 and isinstance(record.get("v"), dict):
        value = record["v"]
        if isinstance(value.get("requests"), list) and "sessionId" in value:
            return "copilot"
    if isinstance(record.get("requests"), list) and "sessionId" in record:
        return "copilot"
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
                model=str(rec.get("model") or "unknown"), client=str(rec.get("client") or ""),
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
                    model=str(message.get("model") or "unknown"),
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
    model = "unknown"
    for rec in records:
        timestamp = parse_ts(rec.get("timestamp"))
        rtype = rec.get("type")
        payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else rec
        ptype = payload.get("type")
        if rtype == "session_meta":
            session = str(payload.get("session_id") or payload.get("id") or session)
        if rtype == "turn_context":
            model = str(payload.get("model") or "unknown")
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
                model=model,
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


def replay_copilot_journal(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    state: Any = None
    for entry in records:
        kind = entry.get("kind")
        if kind == 0:
            state = entry.get("v")
            continue
        path = entry.get("k")
        if state is None or kind not in (1, 2, 3) or not isinstance(path, list):
            raise ValueError("Invalid Copilot journal operation")
        if not path:
            if kind == 2:
                raise ValueError("Invalid Copilot array path")
            continue
        try:
            parent = state
            for key in path[:-1]:
                if not isinstance(key, (str, int)) or isinstance(key, bool) or isinstance(key, int) and key < 0:
                    raise ValueError("Invalid Copilot journal path")
                parent = parent[key]
            key = path[-1]
            if not isinstance(key, (str, int)) or isinstance(key, bool) or isinstance(key, int) and key < 0:
                raise ValueError("Invalid Copilot journal path")
            if kind == 3:
                if isinstance(parent, dict):
                    parent.pop(key, None)
                else:
                    parent[key] = None
            elif kind == 1:
                if isinstance(parent, list) and key == len(parent):
                    parent.append(entry.get("v"))
                else:
                    parent[key] = entry.get("v")
            else:
                array = parent.get(key) if isinstance(parent, dict) else parent[key]
                array = [] if array is None else array
                values = entry.get("v", [])
                index = entry.get("i", len(array))
                if not isinstance(array, list) or not isinstance(values, list) or type(index) is not int or not 0 <= index <= len(array):
                    raise ValueError("Invalid Copilot journal array update")
                del array[index:]
                array.extend(values)
                parent[key] = array
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError("Invalid Copilot journal path") from exc
    if not isinstance(state, dict) or not isinstance(state.get("requests"), list):
        raise ValueError("Missing Copilot session snapshot")
    return state


def parse_copilot(records: Iterable[dict[str, Any]], session: str, source_file: str, stats: ParseStats) -> list[ToolCall]:
    calls: dict[tuple[str, str, str], ToolCall] = {}
    for record in records:
        session_id = str(record.get("sessionId") or session)
        stats.copilot_sessions[session_id] += 1
        for request in record.get("requests", []):
            if not isinstance(request, dict):
                continue
            result = request.get("result")
            metadata = result.get("metadata") if isinstance(result, dict) else None
            if not isinstance(metadata, dict):
                continue
            rounds = metadata.get("toolCallRounds")
            results = metadata.get("toolCallResults")
            if not isinstance(rounds, list):
                continue
            results = results if isinstance(results, dict) else {}
            response = request.get("response")
            invocations = {part.get("toolCallId"): part for part in response
                           if isinstance(part, dict) and part.get("kind") == "toolInvocationSerialized"} if isinstance(response, list) else {}
            for round_data in rounds:
                if not isinstance(round_data, dict) or not isinstance(round_data.get("toolCalls"), list):
                    continue
                for item in round_data["toolCalls"]:
                    if not isinstance(item, dict) or not item.get("id") or not item.get("name"):
                        continue
                    call_id = str(item["id"])
                    server, tool = tool_identity(str(item["name"]))
                    invocation = invocations.get(call_id, {})
                    if not invocation:
                        base_id = re.sub(r"__vscode-\d+$", "", call_id)
                        candidate = invocations.get(base_id, {})
                        if candidate.get("toolId") == item["name"]:
                            invocation = candidate
                    origin = invocation.get("source")
                    if isinstance(origin, dict) and origin.get("type") == "mcp":
                        label = origin.get("serverLabel") or origin.get("label")
                        if isinstance(label, str) and label:
                            server, tool = label, str(item["name"])
                    call = ToolCall(source="copilot", client="copilot", session=session_id,
                                    call_id=call_id, server=server, tool=tool, source_file=source_file,
                                    model=str(request.get("modelId") or "unknown"),
                                    arguments=parse_arguments(item.get("arguments")))
                    if call_id in results:
                        output = results[call_id]
                        content = output.get("content") if isinstance(output, dict) else None
                        if isinstance(content, list):
                            text_parts = []
                            for part in content:
                                if isinstance(part, dict) and "mimeType" in part and "data" in part:
                                    payload = part["data"]
                                    call.media_parts.append({
                                        "mime_type": str(part["mimeType"]),
                                        "encoded_chars": len(payload) if isinstance(payload, str) else None,
                                        "token_cost": None,
                                    })
                                else:
                                    text_parts.append(flatten_text(part.get("value", part) if isinstance(part, dict) else part))
                            call.result = "\n".join(text_parts)
                        else:
                            call.result = output
                        call.error = isinstance(output, dict) and output.get("isError") is True
                    calls[(session_id, str(request.get("requestId") or ""), call_id)] = call
    return list(calls.values())


def load_file(path: pathlib.Path, forced_source: str, stats: ParseStats) -> list[ToolCall]:
    if path.suffix.lower() == ".json":
        stats.files += 1
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if isinstance(value, dict):
            value = value.get("records", value.get("messages", [value]))
        records = [record for record in value if isinstance(record, dict)] if isinstance(value, list) else []
    else:
        iterator = iter_jsonl(path, stats)
        first = next(iterator, None)
        if first is not None and first.get("kind") == 0 and sniff_source(first) == "copilot":
            malformed_before = stats.malformed_lines
            records = [replay_copilot_journal(itertools.chain([first], iterator))]
            if stats.malformed_lines != malformed_before:
                raise ValueError("Incomplete or malformed Copilot journal")
        else:
            records = [first, *iterator] if first is not None else []
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
    if source == "copilot":
        return parse_copilot(records, session, str(path), stats)
    return []


def read_client_config(path: pathlib.Path) -> Any:
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError("configuration exceeds size limit")
    text = path.read_text(encoding="utf-8-sig")
    if path.suffix == ".toml":
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib
        return tomllib.loads(text)
    if path.suffix in {".yaml", ".yml"}:
        import yaml
        return yaml.safe_load(text)
    text = re.sub(r'"(?:\\.|[^"\\])*"|//[^\n\r]*|/\*.*?\*/',
                  lambda match: match.group() if match.group().startswith('"') else " ", text, flags=re.S)
    text = re.sub(r'("(?:\\.|[^"\\])*")|,\s*(?=[}\]])',
                  lambda match: match.group(1) or "", text)
    return json.loads(text) if text.strip() else {}


def discover_clients(source: str = "auto") -> dict[str, Any]:
    home = pathlib.Path.home()
    workspace = pathlib.Path.cwd()
    roaming = pathlib.Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    xdg = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    app_roots = [roaming, xdg, home / "Library" / "Application Support"]
    clients: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, str]] = []
    scanned: set[tuple[str, pathlib.Path]] = set()
    config_seen: set[tuple[str, pathlib.Path]] = set()
    history_seen: set[pathlib.Path] = set()
    roots = {client_id: pathlib.Path(os.environ.get(spec.get("env", ""), "").strip()
             or (home / spec["home"] if "home" in spec else xdg / spec["config"]))
             for client_id, spec in CLIENT_REGISTRY.items() if "home" in spec or "config" in spec}

    def client_for(client_id: str) -> dict[str, Any]:
        if client_id not in clients:
            clients[client_id] = {"id": client_id, "name": CLIENT_REGISTRY[client_id]["name"], "artifacts": [],
                                  "mcp_servers": [], "models": [], "history": []}
        return clients[client_id]

    def artifact(client_id: str, kind: str, path: pathlib.Path) -> None:
        item = {"kind": kind, "path": str(path)}
        items = client_for(client_id)["artifacts"]
        if item not in items:
            items.append(item)

    def config(client_id: str, path: pathlib.Path, standalone_mcp: bool = False) -> None:
        if not path.is_file() or (client_id, path) in config_seen:
            return
        config_seen.add((client_id, path))
        artifact(client_id, "MCP configuration" if standalone_mcp else "config", path)
        try:
            value = read_client_config(path)
        except ImportError:
            dependency = "PyYAML" if path.suffix in {".yaml", ".yml"} else "tomli (Python 3.10)"
            warnings.append({"path": str(path), "message": f"Config detected but not parsed: install {dependency}."})
            return
        except Exception as exc:
            warnings.append({"path": str(path), "message": f"Config unreadable or unsupported ({type(exc).__name__})."})
            return
        if not isinstance(value, dict):
            return
        client = client_for(client_id)

        def summarize(settings: dict[str, Any]) -> None:
            for key in ("model", "models", "modelConfigurations"):
                models = settings.get(key, [])
                if not isinstance(models, list):
                    models = [models]
                for model in models:
                    identifier = model.get("model") if isinstance(model, dict) else model
                    if isinstance(identifier, str) and identifier:
                        item = {"model": identifier, "evidence": "configured", "path": str(path)}
                        if item not in client["models"]:
                            client["models"].append(item)
            containers = [settings.get("mcpServers"), settings.get("mcp_servers")]
            containers.append(settings.get("amp.mcpServers"))
            mcp = settings.get("mcp")
            if isinstance(mcp, dict):
                containers.append(mcp.get("servers", mcp if client_id == "opencode" else None))
            if standalone_mcp:
                containers.append(settings.get("servers"))
            for servers in containers:
                if isinstance(servers, dict):
                    entries = servers.items()
                elif isinstance(servers, list):
                    entries = ((server.get("name", "unknown"), server) for server in servers if isinstance(server, dict))
                else:
                    continue
                for name, server in entries:
                    if not isinstance(server, dict):
                        continue
                    transport = server.get("type", server.get("transport"))
                    if not isinstance(transport, str) or transport not in {"stdio", "http", "sse", "streamable-http", "streamableHttp"}:
                        transport = "stdio" if server.get("command") else "http" if server.get("url") else "unknown"
                    item = {"server": str(name), "transport": transport, "evidence": "configured",
                            "disabled": bool(server.get("disabled")) or server.get("enabled") is False, "path": str(path)}
                    if item not in client["mcp_servers"]:
                        client["mcp_servers"].append(item)

        summarize(value)
        projects = value.get("projects")
        if isinstance(projects, dict):
            for settings in projects.values():
                if isinstance(settings, dict):
                    summarize(settings)

    def scan(client_id: str, root: pathlib.Path, kind: str) -> None:
        if not root.is_dir() or root.is_symlink() or (client_id, root) in scanned:
            return
        scanned.add((client_id, root))
        artifact(client_id, kind, root)
        count = 0
        def walk_error(error: OSError) -> None:
            warnings.append({"path": str(root), "message": f"Directory inaccessible ({type(error).__name__})."})
        for directory, folders, files in os.walk(root, onerror=walk_error, followlinks=False):
            directory_path = pathlib.Path(directory)
            category = {"globalStorage": "global storage", "workspaceStorage": "workspace storage",
                        "chatSessions": "chat/agent state", "sessions": "sessions", "projects": "projects"}.get(directory_path.name)
            if category:
                artifact(client_id, category, directory_path)
            folders[:] = sorted(folder for folder in folders if folder not in {
                "node_modules", ".git", "Cache", "CachedData", "CachedExtensionVSIXs", "__pycache__", "skills",
            } and not (pathlib.Path(directory) / folder).is_symlink())
            for filename in sorted(files):
                count += 1
                if count > 10000:
                    warnings.append({"path": str(root), "message": "Discovery limit reached (10000 files); results are partial."})
                    return
                path = pathlib.Path(directory) / filename
                if path.is_symlink():
                    continue
                owner = client_id
                for extension, extension_client in (("continue.continue", "continue"), ("saoudrizwan.claude-dev", "cline"), ("rooveterinaryinc.roo-cline", "roo")):
                    if extension in str(path).lower():
                        owner = extension_client
                if filename in {"settings.json", "settings.local.json", "config.json", "config.toml", "config.yaml", "config.yml", ".claude.json", "opencode.json", "opencode.jsonc"}:
                    config(owner, path)
                elif filename in {"mcp.json", ".mcp.json", "mcp-config.json", "mcp_config.json", "claude_desktop_config.json", "cline_mcp_settings.json", "roo_mcp_settings.json"}:
                    config({"cline_mcp_settings.json": "cline", "roo_mcp_settings.json": "roo"}.get(filename, owner), path, True)
                elif path.parent.name == "mcpServers" and path.suffix in {".yaml", ".yml", ".json"}:
                    config(owner, path, True)
                if kind == "plugin definitions":
                    continue
                relative = str(path.relative_to(root)).lower()
                history_location = kind in {"sessions", "projects", "agent history"} or any(
                    token in relative for token in ("session", "history", "chat", "conversation"))
                is_history = path.suffix in {".jsonl", ".json"} and history_location
                opaque = path.suffix in {".vscdb", ".db", ".sqlite"}
                if (is_history or opaque) and path not in history_seen and (owner, path) not in config_seen:
                    history_seen.add(path)
                    client_for(owner)["history"].append({"path": str(path), "format": path.suffix[1:],
                                                        "status": "opaque-storage" if opaque else "pending"})

    def scan_editor(client_id: str, user_root: pathlib.Path) -> None:
        if user_root.is_dir():
            artifact(client_id, "editor storage", user_root)
        config(client_id, user_root / "settings.json")
        config(client_id, user_root / "mcp.json", True)
        for folder, kind in (("workspaceStorage", "workspace storage"), ("profiles", "profiles"), ("globalStorage", "global storage")):
            scan(client_id, user_root / folder, kind)

    if source in {"auto", "claude"}:
        root = roots["claude"]
        if root.is_dir():
            artifact("claude", "client root", root)
        for filename in ("settings.json", "settings.local.json", "mcp.json"):
            config("claude", root / filename, filename == "mcp.json")
        config("claude", home / ".claude.json")
        for folder, kind in (("sessions", "sessions"), ("projects", "projects"), ("ide", "IDE state"), ("plugins", "plugin definitions")):
            scan("claude", root / folder, kind)
        config("claude", workspace / ".mcp.json", True)
        config("claude", workspace / ".claude" / "settings.json")
        config("claude", workspace / ".claude" / "settings.local.json")
    if source in {"auto", "codex"}:
        root = roots["codex"]
        if root.is_dir():
            artifact("codex", "client root", root)
        config("codex", root / "config.toml")
        for folder in ("sessions", "archived_sessions"):
            scan("codex", root / folder, "sessions")
        config("codex", workspace / ".codex" / "config.toml")
    if source == "auto":
        for client_id, root in roots.items():
            if client_id not in {"claude", "codex"}:
                scan(client_id, root, "client root")
        for client_id, folder in (("cursor", ".cursor"), ("continue", ".continue"), ("gemini", ".gemini"), ("roo", ".roo"), ("kiro", ".kiro")):
            scan(client_id, workspace / folder, "workspace config")
        for base in app_roots:
            for app, client_id in (("Cursor", "cursor"), ("Code", "copilot"), ("Code - Insiders", "copilot"), ("VSCodium", "copilot")):
                scan_editor(client_id, base / app / "User")
            config("other", base / "Claude" / "claude_desktop_config.json", True)
        for folder in (".vscode-server", ".vscode-server-insiders"):
            scan_editor("copilot", home / folder / "data" / "User")
        config("copilot", workspace / ".vscode" / "mcp.json", True)
        config("copilot", workspace / ".vscode" / "settings.json")
        config("cursor", workspace / ".cursor" / "mcp.json", True)
        config("continue", workspace / ".continue" / "config.json")
        config("continue", workspace / ".continue" / "config.yaml")
        scan("continue", workspace / ".continue" / "mcpServers", "MCP configuration")
        scan("other", home / ".tooltax" / "traces", "agent history")
    return {"clients": list(clients.values()), "warnings": warnings,
            "scope": "Known local client roots and current workspace; read-only, no servers started or contacted."}


def correlate(calls: list[ToolCall], discovery: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    counts = Counter((call.model, call.client or call.source, call.server, call.tool) for call in calls)
    rows = [{"model": model, "client": client, "server": server, "tool": tool,
             "calls": count, "evidence": "observed"}
            for (model, client, server, tool), count in sorted(counts.items())]
    if discovery:
        for client in discovery["clients"]:
            for model in sorted({item["model"] for item in client["models"] if item["evidence"] == "configured"}):
                rows.append({"model": model, "client": client["id"], "server": None, "tool": None,
                             "calls": 0, "evidence": "configured-model"})
            for server in client["mcp_servers"]:
                rows.append({"model": "unknown", "client": client["id"], "server": server["server"],
                             "tool": None, "calls": 0, "evidence": "configured", "transport": server["transport"],
                             "disabled": server["disabled"]})
    return rows


def audit_customizations(calls: list[ToolCall], discovery: dict[str, Any] | None = None,
                         source: str = "auto", share_safe: bool = False) -> dict[str, Any]:
    home, workspace = pathlib.Path.home(), pathlib.Path.cwd()
    xdg = pathlib.Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    roots: list[tuple[pathlib.Path, str, str]] = [(workspace, "workspace", "unknown")]
    roots.append((home / ".agents", "user", "shared"))
    roots.extend((home / name, "user", "shared") for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"))
    for client, spec in CLIENT_REGISTRY.items():
        if source != "auto" and client != source:
            continue
        if "home" not in spec and "config" not in spec:
            continue
        root = pathlib.Path(os.environ.get(spec.get("env", ""), "").strip()
                            or (home / spec["home"] if "home" in spec else xdg / spec["config"]))
        roots.extend((root / folder, "user", client) for folder in ("skills", "agents", "instructions", "prompts", "rules", "steering"))
        roots.extend((root / name, "user", client) for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md"))
    if source == "auto":
        roaming = pathlib.Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
        for base in (roaming, xdg, home / "Library" / "Application Support"):
            for editor in ("Code", "Code - Insiders", "VSCodium", "Cursor"):
                roots.extend((base / editor / "User" / folder, "user", "cursor" if editor == "Cursor" else "copilot")
                             for folder in ("prompts", "profiles"))
        for client in (discovery or {}).get("clients", []):
            for artifact in client["artifacts"]:
                if artifact["kind"] == "editor storage":
                    root = pathlib.Path(artifact["path"])
                    roots.extend((root / folder, "user", client["id"]) for folder in ("prompts", "profiles"))
        for dirname in (".vscode", ".vscode-insiders", ".vscode-server", ".cursor"):
            extensions = home / dirname / "extensions"
            if extensions.is_dir() and not extensions.is_symlink():
                for extension in sorted(extensions.iterdir()):
                    if extension.is_symlink():
                        continue
                    for folder in ("skills", "agent-skills", "ext/agent-skills", "src/lm/skills", "resources/skills", "ai-mlstudio/resources/skills", "agents", "prompts"):
                        roots.append((extension / folder, "extension", "cursor" if dirname == ".cursor" else "copilot"))
    files: list[dict[str, Any]] = []
    warnings: list[dict[str, str]] = []
    seen: set[pathlib.Path] = set()
    fingerprints: dict[str, list[str]] = defaultdict(list)
    rules: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    observed_reads: dict[pathlib.Path, Counter] = defaultdict(Counter)
    observed_tools = Counter((call.server, call.tool) for call in calls)
    for call in calls:
        if call.server != "builtin" or call.tool.lower() not in {"read_file", "read"} or call.error or call.explicit_success is False or call.result is None or call.empty_result:
            continue
        if isinstance(call.arguments, dict):
            raw = next((call.arguments[key] for key in ("filePath", "file_path", "path") if isinstance(call.arguments.get(key), str)), None)
            if raw and pathlib.Path(raw).is_absolute():
                observed_reads[pathlib.Path(raw).resolve()][(call.client or call.source, call.model)] += 1
    budget_bytes = 0
    excluded = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build", "vendor", "workspaceStorage", "globalStorage", "sessions", "projects"}

    def inspect(path: pathlib.Path, scope: str, client: str) -> None:
        nonlocal budget_bytes
        name = path.name.lower()
        kind = ("skill" if name == "skill.md" else "agent" if name.endswith(".agent.md") else
                "prompt" if name.endswith(".prompt.md") else "instructions" if name in {
                    "agents.md", "agents.override.md", "claude.md", "gemini.md", "copilot-instructions.md", ".cursorrules", ".windsurfrules",
                } or name.endswith((".instructions.md", ".mdc")) else None)
        if kind is None and path.suffix.lower() == ".md" and any(part in {"rules", "instructions", "agents", "steering"} for part in path.parts):
            kind = "agent" if "agents" in path.parts else "instructions"
        if kind is None or path.is_symlink() or path.resolve() in seen:
            return
        seen.add(path.resolve())
        label = "<redacted:path>" if share_safe else str(path)
        item: dict[str, Any] = {"id": f"customization-{len(files) + 1}", "path": label, "kind": kind,
                                "scope": scope, "client": client, "evidence": "installed",
                                "applicability": "unknown", "status": "readable", "signals": []}
        files.append(item)
        try:
            available_bytes = min(4 * 1024 * 1024, 32 * 1024 * 1024 - budget_bytes)
            if path.stat().st_size > available_bytes or available_bytes <= 0:
                item["status"] = "size-limit"
                return
            with path.open("rb") as handle:
                content = handle.read(available_bytes + 1)
            if len(content) > available_bytes:
                item["status"] = "size-limit"
                return
            budget_bytes += len(content)
            text = content.decode("utf-8-sig")
        except (OSError, UnicodeError) as exc:
            item["status"] = "unreadable"
            warnings.append({"path": label, "message": f"Customization unreadable ({type(exc).__name__})."})
            return
        item.update(bytes=len(content), estimated_tokens=token_estimate(text), lines=len(text.splitlines()))
        item["observed_reads"] = [{"client": owner, "model": model, "calls": count, "evidence": "observed-file-read"}
                                  for (owner, model), count in sorted(observed_reads[path.resolve()].items())]
        metadata: dict[str, Any] = {}
        body = text
        item["frontmatter_status"] = "none"
        sections = text.splitlines()
        if sections and sections[0].strip() == "---":
            try:
                end = next(index for index, line in enumerate(sections[1:], 1) if line.strip() == "---")
                body = "\n".join(sections[end + 1:])
                import yaml
                parsed = yaml.safe_load("\n".join(sections[1:end]))
                if parsed is not None and not isinstance(parsed, dict):
                    raise ValueError("Frontmatter must be a mapping")
                metadata = parsed or {}
                item["frontmatter_status"] = "parsed"
            except ImportError:
                item["frontmatter_status"] = "parser-unavailable"
            except Exception:
                item["frontmatter_status"] = "invalid"
                item["signals"].append("invalid-frontmatter")
        apply_to = metadata.get("applyTo", metadata.get("globs"))
        if metadata.get("alwaysApply") is True or (isinstance(apply_to, str) and apply_to.strip() in {"*", "**", "**/*"}):
            item["applicability"] = "declared-broad"
            item["signals"].append("broad-scope")
        elif apply_to:
            item["applicability"] = "declared-scoped"
        elif kind in {"skill", "agent", "prompt"}:
            item["applicability"] = "on-demand-candidate"
        if item["estimated_tokens"] > 2000:
            item["signals"].append("large-customization")
        paragraphs = Counter(part.strip() for part in re.split(r"\n\s*\n", body) if len(part.strip()) >= 120)
        item["repeated_paragraphs"] = sum(count - 1 for count in paragraphs.values() if count > 1)
        if item["repeated_paragraphs"]:
            item["signals"].append("repeated-paragraphs")
        if text.strip():
            fingerprints[short_hash(text.strip())].append(item["id"])
        item["tool_mentions"] = [{"server": server, "tool": tool, "observed_calls": count, "evidence": "text-mention-not-causation"}
                                 for (server, tool), count in sorted(observed_tools.items())
                                 if re.search(r"(?<![\w])" + re.escape(tool) + r"(?![\w])", text)]
        for line_number, line in enumerate(sections, 1):
            for match in re.finditer(r"\b(always|never)\s+(use|run|call|invoke)\s+`?([a-zA-Z][\w.-]*)", line, re.I):
                polarity, verb, target = (part.lower() for part in match.groups())
                if target not in {"a", "an", "the", "this", "that", "it", "any", "all"}:
                    rules[(verb, target)][polarity].append({"file": item["id"], "line": line_number})

    for root, scope, client in roots:
        if root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
            continue
        if root.is_file():
            inspect(root, scope, client)
            continue
        if not root.is_dir():
            continue
        count = 0
        def walk_error(error: OSError) -> None:
            warnings.append({"path": "<redacted:path>" if share_safe else str(root), "message": f"Customization directory inaccessible ({type(error).__name__})."})
        for directory, folders, names in os.walk(root, onerror=walk_error, followlinks=False):
            folders[:] = sorted(folder for folder in folders if folder not in excluded and not (pathlib.Path(directory) / folder).is_symlink())
            count += len(names)
            if count > 10000:
                warnings.append({"path": "<redacted:path>" if share_safe else str(root), "message": "Customization scan limit reached (10000 files); results are partial."})
                break
            for name in sorted(names):
                inspect(pathlib.Path(directory) / name, scope, client)
    duplicates = [identifiers for identifiers in fingerprints.values() if len(identifiers) > 1]
    conflicts = [{"rule": f"{verb} {target}", "always": evidence["always"], "never": evidence["never"],
                  "evidence": "possible-conflict-scope-unverified"}
                 for (verb, target), evidence in sorted(rules.items()) if evidence["always"] and evidence["never"]]
    return {"files": files, "duplicate_groups": duplicates, "possible_conflicts": conflicts, "warnings": warnings,
            "static_tokens": sum(item.get("estimated_tokens", 0) for item in files),
            "notes": ["Static file sizes are not added to usage or waste totals. Installed files may never be loaded.",
                      "Declared scope is not verified applicability; successful file reads do not prove instruction injection or compliance.",
                      "Tool mentions and simple always/never rule conflicts are review candidates, not causal or semantic proof.",
                      "Current file contents may differ from the version read in a historical trace. Frontmatter requires optional PyYAML."]}


def discover_paths(inputs: list[str], source: str) -> list[pathlib.Path]:
    paths: list[pathlib.Path] = []
    if not inputs:
        discovery = discover_clients(source)
        paths.extend(pathlib.Path(item["path"]) for client in discovery["clients"]
                     for item in client["history"] if item["status"] == "pending")
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
    recent_by_session: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for call in calls:
        recent = recent_by_session[(call.source, call.client, call.session)]
        text = flatten_text(call.result).strip().lower()
        is_error = call.error or call.explicit_success is False
        if is_error:
            findings.append(Finding("error", "high", call, call.observed_tokens, "tool call failed"))
        if call.empty_result and call.result is not None and not is_error:
            findings.append(Finding("empty-result", "medium", call, call.observed_tokens, "tool returned no useful payload"))
        if call.output_tokens >= large_output_tokens:
            excess = call.output_tokens - large_output_tokens
            findings.append(Finding("output-bloat", "medium", call, excess, f"output ~{call.output_tokens:,} estimated payload tokens; potential excess, usefulness unknown"))
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
            "media_attachments": sum(len(call.media_parts) for call in items),
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
    if row["server"] == "builtin" and not row.get("tool"):
        return "review individual tools"
    calls = row["calls"]
    waste = row["waste_pct"]
    score = row["utility_score"]
    errors = row["errors"]
    findings = row["findings"]
    if calls >= 5 and score < 45 and waste >= 35:
        return "consider disabling or replacing"
    if findings.get("duplicate-call", 0) >= max(2, calls // 4):
        return "deduplicate/reuse results"
    if findings.get("output-bloat", 0) >= max(2, math.ceil(calls / 10)):
        return "review large text outputs"
    if errors >= max(2, calls // 3):
        return "investigate reliability"
    if score >= 85 and waste < 20:
        if row.get("media_attachments"):
            return "text signals clear; media unpriced"
        return "keep"
    return "monitor"


def builtin_metrics(calls: list[ToolCall], findings: list[Finding]) -> list[dict[str, Any]]:
    by_tool: dict[str, list[ToolCall]] = defaultdict(list)
    findings_by_tool: dict[str, list[Finding]] = defaultdict(list)
    for call in calls:
        if call.server == "builtin":
            by_tool[call.tool].append(call)
    for finding in findings:
        if finding.call.server == "builtin":
            findings_by_tool[finding.call.tool].append(finding)
    rows = [{**group_metrics(items, findings_by_tool[tool])[0], "tool": tool}
            for tool, items in by_tool.items()]
    return [{**row, "recommendation": recommendation(row)} for row in rows]


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
        "media_attachments": sum(len(call.media_parts) for call in calls),
        "media_encoded_chars": sum(part["encoded_chars"] or 0 for call in calls for part in call.media_parts),
        "media_token_cost": None,
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
        "builtin_tools": builtin_metrics(calls, findings),
        "findings": [
            {
                "kind": f.kind, "severity": f.severity, "server": f.call.server, "tool": f.call.tool,
                "call_id": f.call.call_id, "waste_tokens": f.waste_tokens, "detail": f.detail,
                "source_file": "<redacted:path>" if share_safe else f.call.source_file,
            }
            for f in findings
        ],
        "share_safe": share_safe,
        "correlations": correlate(calls),
        "notes": [
            "Copilot typed attachments are excluded from text estimates; media token cost is unknown, not zero.",
            "Waste fields are retained for compatibility: they measure potential excess and heuristic signals, not proven waste.",
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


def print_local_context(data: dict[str, Any], limit: int = 20, markdown: bool = False) -> None:
    discovery = data.get("discovery")
    rows = data.get("correlations", [])
    if not discovery and not rows:
        return
    if markdown:
        print("```text")
    if discovery:
        print("ToolTax local discovery")
        print("=" * 68)
        if not discovery["clients"]:
            print("No clients detected in known locations.")
        for client in discovery["clients"]:
            print(f"\n{client['name']}")
            by_kind: dict[str, list[str]] = defaultdict(list)
            for item in client["artifacts"]:
                by_kind[item["kind"]].append(item["path"])
            for kind, paths in by_kind.items():
                suffix = f" (+{len(paths) - 1} more)" if len(paths) > 1 else ""
                print(f"  {kind}: {paths[0]}{suffix}")
            for model in sorted({(item["model"], item["evidence"]) for item in client["models"]}):
                print(f"  model: {model[0]} ({model[1]})")
            print(f"  MCP definitions: {len(client['mcp_servers'])}")
            print(f"  observed activity: {client.get('observed_calls', 0):,} calls, {len(client.get('observed_servers', []))} MCP servers")
            for server in client["mcp_servers"][:limit]:
                disabled = ", disabled" if server["disabled"] else ""
                print(f"    {server['server']}: {server['transport']} (configured{disabled})")
            statuses = Counter(item["status"] for item in client["history"])
            print("  transcripts/history: " + (", ".join(f"{key}={value}" for key, value in sorted(statuses.items())) or "none found"))
        print(f"\n{discovery['scope']}")
        print("Configured models/servers do not prove usage. Opaque storage and unsupported history are not scored.")
        for warning in discovery["warnings"][:limit]:
            print(f"Warning: {warning['path']}: {warning['message']}")
        if len(discovery["warnings"]) > limit:
            print(f"{len(discovery['warnings']) - limit} more warnings; use --format json for full details.")
    if rows:
        print("\nMODEL -> CLIENT -> MCP SERVER -> TOOL")
        tree: dict[str, Any] = {}
        for row in rows:
            tree.setdefault(row["model"], {}).setdefault(row["client"], {}).setdefault(row["server"], []).append(row)
        for model, clients in tree.items():
            print(model)
            for client, servers in clients.items():
                print(f"  {client}")
                for server, tools in list(servers.items())[:limit]:
                    print(f"    {server or 'MCP server unknown'}")
                    for tool in tools[:limit]:
                        if tool["evidence"] == "observed":
                            print(f"      {tool['tool']}: {tool['calls']} observed call(s)")
                        elif tool["evidence"] == "configured-model":
                            print("      tools unknown (configured model; no observed model/server association)")
                        else:
                            disabled = ", disabled" if tool["disabled"] else ""
                            print(f"      tools unknown (configured {tool['transport']}{disabled}; model association unknown)")
                    if len(tools) > limit:
                        print(f"      ... {len(tools) - limit} more tools; use --format json")
                if len(servers) > limit:
                    print(f"    ... {len(servers) - limit} more servers; use --format json")
    if markdown:
        print("```")
    print()


def print_customizations(data: dict[str, Any], markdown: bool = False, limit: int = 10) -> None:
    audit = data.get("customizations")
    if audit is None:
        return
    if markdown:
        print("```text")
    print("\nCUSTOMIZATION AUDIT (static files, not billed usage)")
    files = audit["files"]
    reads = sum(bool(item.get("observed_reads")) for item in files)
    print(f"Files: {len(files)} | Static size: ~{audit['static_tokens']:,} tok | Observed file reads: {reads} files")
    print(f"Identical-content groups: {len(audit['duplicate_groups'])} | Possible opposing rules: {len(audit['possible_conflicts'])}")
    for item in sorted(files, key=lambda item: (-len(item["signals"]), -item.get("estimated_tokens", 0), item["id"]))[:limit]:
        signals = ", ".join(item["signals"]) or "no static signals"
        print(f"  {item['id']} {item['kind']}: ~{item.get('estimated_tokens', 0):,} tok | {item['applicability']} | {signals} | {item['status']}")
        print(f"    {item['path']}")
    for group in audit["duplicate_groups"][:limit]:
        print("  Identical content: " + ", ".join(group))
    for conflict in audit["possible_conflicts"][:limit]:
        locations = ", ".join(f"{polarity} {entry['file']}:{entry['line']}" for polarity in ("always", "never") for entry in conflict[polarity][:3])
        print(f"  Possible opposing rule ({conflict['rule']}): {locations}; scope overlap unverified")
    for warning in audit["warnings"][:limit]:
        print(f"  Warning: {warning['path']}: {warning['message']}")
    print("Installed is not loaded. Declared scope is not verified applicability. Use --format json for all files and evidence.")
    if markdown:
        print("```")
    print()


def print_text(data: dict[str, Any], limit: int = 20, interactive: bool = False, color: bool = False) -> None:
    if interactive:
        print(terminal_color("\n  T O O L T A X   /   LOCAL AUDIT", "1;36", color))
        print(terminal_color("  " + "=" * min(54, max(1, shutil.get_terminal_size().columns - 3)), "36", color))
    print_local_context(data, limit)
    print_customizations(data)
    print("TOOLTAX — AGENT TOOL COST REPORT")
    print("=" * 68)
    print(f"Calls: {data['calls']:,}   Estimated payload: ~{data['observed_tokens']:,} tok   Potential excess/signals: ~{data['estimated_waste_tokens']:,} tok ({data['estimated_waste_pct']:.1f}%)")
    if data.get("media_attachments"):
        print(f"Media: {data['media_attachments']:,} attachments excluded from text estimates; token cost unknown.")
    print(f"Files: {data['files_scanned']:,}   Sources: {', '.join(f'{k}:{v}' for k,v in data['sources'].items()) or 'none'}")
    if data.get("schema_files"):
        print(f"Advertised schema: ~{data['advertised_schema_tokens']:,} tok across {data['schema_files']} capture file(s)   Median latency: {fmt_int(data.get('median_latency_ms'))} ms")
    print()
    if not data["servers"]:
        print("No supported tool calls found.")
        if data.get("discovery"):
            print("Client state is listed above. Export a supported trace or use tool_tax_capture.py for measurable MCP activity.")
        return
    rows = display_rows(data)
    width = max([24, *(len(row['label']) for row in rows[:limit])])
    compact = interactive and shutil.get_terminal_size((80, 24)).columns < width + 85
    if not compact:
        print(f"{'SERVER / BUILTIN TOOL':<{width}} {'CALLS':>6} {'TOKENS':>10} {'SIGNALS':>8} {'SCORE':>7}  RECOMMENDATION")
        print("-" * 82)
    for row in rows[:limit]:
        if compact:
            columns = max(10, shutil.get_terminal_size((80, 24)).columns - 2)
            filled = max(0, min(10, round(row['utility_score'] / 10)))
            bar = "[" + "#" * filled + "." * (10 - filled) + "]"
            print(terminal_color(textwrap.fill(row['label'], width=columns), "1", color))
            metrics = f"{bar} {row['utility_score']:.0f}/100 | {row['calls']:,} calls | ~{row['observed_tokens']:,} tok"
            print(terminal_color(textwrap.fill(metrics, width=columns), "32" if row['utility_score'] >= 85 else "33", color))
            print(textwrap.fill(f"{row['waste_pct']:.1f}% signals | {row['recommendation']}", width=columns))
            print()
        else:
            line = f"{row['label']:<{width}} {row['calls']:>6} {row['observed_tokens']:>10,} {row['waste_pct']:>7.1f}% {row['utility_score']:>6.0f}/100  {row['recommendation']}"
            print(terminal_color(line, "32" if row['utility_score'] >= 85 else "33", color))
    if len(rows) > limit:
        print(f"{len(rows) - limit} more rows; use --format json or --format markdown for all tools.")
    print()
    top = sorted(data["findings"], key=lambda f: (-f["waste_tokens"], f["server"], f["tool"]))[:8]
    if top:
        print("TOP POTENTIAL EXCESS / REVIEW SIGNALS")
        for f in top:
            print(f"- {f['server']}/{f['tool']}: {f['kind']} (~{f['waste_tokens']:,} tok) — {f['detail']}")
        print()
    print("Interpretation: size thresholds are review signals, not proven waste. Provider prompt/schema/media/billing cost is not reconstructed.")


def display_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [{**row, "label": row["server"]} for row in data["servers"] if row["server"] != "builtin"]
    rows.extend({**row, "label": f"builtin/{row['tool']}"} for row in data.get("builtin_tools", []))
    return sorted(rows, key=lambda row: (-row["waste_tokens"], -row["observed_tokens"], row["label"]))


def print_markdown(data: dict[str, Any]) -> None:
    print("# ToolTax report\n")
    print_local_context(data, markdown=True)
    print_customizations(data, markdown=True)
    print(f"**{data['calls']:,} calls** × **~{data['observed_tokens']:,} estimated payload tokens** × **~{data['estimated_waste_tokens']:,} potential excess/signals ({data['estimated_waste_pct']:.1f}%)**\n")
    if data.get("media_attachments"):
        print(f"Media: {data['media_attachments']:,} attachments excluded from text estimates; token cost unknown.\n")
    print("| Server / Builtin tool | Calls | Tokens | Signals | Utility | Recommendation |")
    print("|---|---:|---:|---:|---:|---|")
    for row in display_rows(data):
        print(f"| `{row['label']}` | {row['calls']} | {row['observed_tokens']:,} | {row['waste_pct']:.1f}% | {row['utility_score']:.0f}/100 | {row['recommendation']} |")
    print("\n## Potential excess / review signals\n")
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


def analyze_inputs(inputs: list[str], args: argparse.Namespace, progress: TerminalProgress | None = None) -> dict[str, Any]:
    stats = ParseStats()
    if progress:
        progress.update("Discovering clients and MCP configuration" if not inputs else "Finding trace files")
    discovery = discover_clients(args.source) if not inputs else None
    history = {pathlib.Path(item["path"]): (client, item) for client in discovery["clients"]
               for item in client["history"] if item["status"] == "pending"} if discovery else {}
    files = sorted(history, key=str) if discovery is not None else discover_paths(inputs, args.source)
    supported_files = []
    calls: list[ToolCall] = []
    copilot_versions: dict[str, tuple[tuple[int, bool], list[ToolCall], dict[str, Any]]] = {}
    for index, path in enumerate(files, 1):
        if progress:
            progress.update(f"Reading traces {index}/{len(files)}")
        try:
            file_info = path.stat()
            if discovery is not None and file_info.st_size > 64 * 1024 * 1024:
                history[path][1]["status"] = "size-limit"
                continue
            file_stats = ParseStats()
            parsed = load_file(path, "auto" if discovery is not None else args.source, file_stats)
            if discovery is not None:
                client, item = history[path]
                recognized = any(key != "unknown" for key in file_stats.source_counts)
                item["status"] = "supported-transcript" if recognized else "metadata-or-unsupported"
                if not recognized:
                    continue
                for call in parsed:
                    call.client = call.client or client["id"]
            for field in dataclasses.fields(stats):
                setattr(stats, field.name, getattr(stats, field.name) + getattr(file_stats, field.name))
            supported_files.append(path)
            if discovery is not None and file_stats.copilot_sessions:
                rank = (file_info.st_mtime_ns, path.suffix == ".jsonl")
                for session_id in file_stats.copilot_sessions:
                    previous = copilot_versions.get(session_id)
                    if previous is None or rank > previous[0]:
                        if previous is not None:
                            previous[2]["status"] = "superseded-session-copy"
                        copilot_versions[session_id] = (rank, [call for call in parsed if call.session == session_id], item)
                    else:
                        item["status"] = "superseded-session-copy"
            else:
                calls.extend(parsed)
        except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
            if discovery is not None:
                history[path][1]["status"] = "unreadable-or-invalid"
                discovery["warnings"].append({"path": str(path), "message": f"History could not be parsed ({type(exc).__name__})."})
            else:
                print(f"warning: {'<redacted:path>' if args.share_safe else path}: {type(exc).__name__}", file=sys.stderr)
    for rank, session_calls, item in copilot_versions.values():
        calls.extend(session_calls)
    if discovery is not None:
        for client in discovery["clients"]:
            observed = [call for call in calls if call.client == client["id"]]
            client["observed_calls"] = len(observed)
            client["observed_servers"] = sorted({call.server for call in observed if call.server != "builtin"})
            for call in observed:
                if call.model != "unknown":
                    model = {"model": call.model, "evidence": "observed", "path": call.source_file}
                    if model not in client["models"]:
                        client["models"].append(model)
    if progress:
        progress.update(f"Scoring {len(calls):,} calls")
    findings = analyze(calls, args.duplicate_window, args.large_output_tokens)
    data = report_data(calls, findings, stats, supported_files, share_safe=args.share_safe)
    if (discovery is not None or getattr(args, "audit_customizations", False)) and not getattr(args, "no_customizations", False):
        if progress:
            progress.update("Auditing skills, agents and instructions")
        data["customizations"] = audit_customizations(calls, discovery, args.source, args.share_safe)
    if discovery is not None:
        data["correlations"] = correlate(calls, discovery)
        if args.share_safe:
            for client in discovery["clients"]:
                for key in ("artifacts", "history", "mcp_servers", "models"):
                    for item in client[key]:
                        item["path"] = "<redacted:path>"
            for warning in discovery["warnings"]:
                warning["path"] = "<redacted:path>"
        data["discovery"] = discovery
    return data

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
    parser.add_argument("paths", nargs="*", help="Trace file(s) or JSONL directories. With no paths, discovers local clients, MCP configuration, and history.")
    parser.add_argument("--source", choices=["auto", "claude", "codex", "canonical"], default="auto", help="Force input adapter (default: auto).")
    parser.add_argument("--format", choices=["text", "json", "markdown"], default="text", help="Output format.")
    parser.add_argument("--output", "-o", help="Write report to a file instead of stdout.")
    parser.add_argument("--duplicate-window", type=int, default=8, help="Calls within which an identical tool+argument call is considered duplicate.")
    parser.add_argument("--large-output-tokens", type=int, default=2000, help="Estimated output token threshold for bloat signal.")
    parser.add_argument("--demo", action="store_true", help="Show a deterministic demo report.")
    parser.add_argument("--before", action="append", default=[], metavar="PATH", help="Baseline trace/file/directory for before/after comparison; repeatable.")
    parser.add_argument("--after", action="append", default=[], metavar="PATH", help="Optimized trace/file/directory for before/after comparison; repeatable.")
    parser.add_argument("--share-safe", action="store_true", help="Suppress local source paths in report output. Use scripts/tool_tax_redact.py to sanitize raw traces before sharing them.")
    customization_options = parser.add_mutually_exclusive_group()
    customization_options.add_argument("--audit-customizations", action="store_true", help="Include local skills/agents/instructions audit with explicit trace paths (automatic during discovery).")
    customization_options.add_argument("--no-customizations", action="store_true", help="Skip the static skills/agents/instructions audit.")
    parser.add_argument("--version", action="version", version=f"ToolTax {VERSION}")
    parser.add_argument("--plain", action="store_true", help="Disable interactive animation, color, and compact terminal styling.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    compare_mode = bool(args.before or args.after)
    if compare_mode and (not args.before or not args.after):
        raise SystemExit("before/after comparison requires at least one --before and one --after path")
    if compare_mode and (args.paths or args.demo):
        raise SystemExit("do not combine positional paths/--demo with --before/--after")

    interactive = (args.format == "text" and not args.output and not args.plain
                   and sys.stdout.isatty() and sys.stderr.isatty()
                   and not os.environ.get("CI") and os.environ.get("TERM") != "dumb")
    color = interactive and "NO_COLOR" not in os.environ
    with TerminalProgress(interactive, color) as progress:
        if compare_mode:
            data = comparison_data(analyze_inputs(args.before, args, progress), analyze_inputs(args.after, args, progress))
        elif args.demo:
            stats = ParseStats()
            calls = demo_calls()
            findings = analyze(calls, args.duplicate_window, args.large_output_tokens)
            data = report_data(calls, findings, stats, [], share_safe=args.share_safe)
        else:
            data = analyze_inputs(args.paths, args, progress)

    def emit() -> None:
        if args.format == "json":
            json.dump(data, sys.stdout, indent=2)
            print()
        elif args.format == "markdown":
            print_comparison_markdown(data) if compare_mode else print_markdown(data)
        else:
            print_comparison_text(data) if compare_mode else print_text(data, interactive=interactive, color=color)

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
