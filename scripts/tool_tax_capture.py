#!/usr/bin/env python3
"""ToolTax live stdio MCP capture proxy.

This process is itself an stdio MCP transport shim: an MCP host launches it instead
of the target server, and ToolTax launches the target server as its child. JSON-RPC
frames are forwarded byte-for-byte while ToolTax writes local JSONL telemetry.

No third-party dependencies. Python 3.10+.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Any, BinaryIO

VERSION = "0.2.0"
SCHEMA = "tooltax.v1"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat(timespec="milliseconds").replace("+00:00", "Z")


def compact_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def token_estimate(value: Any) -> int:
    if value is None:
        return 0
    text = value if isinstance(value, str) else compact_json(value)
    return max(0, math.ceil(len(text) / 4))


def jsonrpc_key(value: Any) -> str:
    """Preserve type so numeric id 1 and string id '1' cannot collide."""
    return compact_json(value)


def sha256_text(value: Any) -> str:
    try:
        raw = compact_json(value)
    except Exception:
        raw = str(value)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


class TraceWriter:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = path.open("a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()

    def write(self, event: dict[str, Any]) -> None:
        with self._lock:
            self._handle.write(compact_json(event) + "\n")

    def close(self) -> None:
        with self._lock:
            self._handle.close()


@dataclass
class PendingRequest:
    method: str
    started_wall: str
    started_monotonic: float
    request_bytes: int
    params: Any = None
    tool_name: str | None = None
    arguments: Any = None


@dataclass
class CaptureStats:
    frames_from_client: int = 0
    frames_from_server: int = 0
    malformed_frames: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    request_bytes: int = 0
    response_bytes: int = 0
    schema_bytes: int = 0
    schema_tokens: int = 0
    tool_count: int = 0
    schema_snapshots: int = 0
    call_input_tokens: int = 0
    call_output_tokens: int = 0
    latencies_ms: list[int] = field(default_factory=list)


class CaptureSession:
    """Observe JSON-RPC without participating in MCP semantics."""

    def __init__(self, server_name: str, writer: TraceWriter):
        self.server_name = server_name
        self.writer = writer
        self.session_id = f"stdio-{int(time.time())}-{os.getpid()}"
        self.pending: dict[str, PendingRequest] = {}
        self.stats = CaptureStats()
        self._tools_by_key: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self.writer.write(
            {
                "schema": SCHEMA,
                "event": "capture_start",
                "timestamp": iso_now(),
                "session": self.session_id,
                "server": self.server_name,
                "transport": "stdio",
                "capture_version": VERSION,
            }
        )

    def _decode(self, raw: bytes) -> dict[str, Any] | None:
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            with self._lock:
                self.stats.malformed_frames += 1
            return None
        return value if isinstance(value, dict) else None

    def observe_client(self, raw: bytes) -> None:
        with self._lock:
            self.stats.frames_from_client += 1
        msg = self._decode(raw)
        if msg is None:
            return

        method = msg.get("method")
        if not isinstance(method, str) or "id" not in msg:
            return

        key = jsonrpc_key(msg.get("id"))
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
        pending = PendingRequest(
            method=method,
            started_wall=iso_now(),
            started_monotonic=time.monotonic(),
            request_bytes=len(raw.rstrip(b"\r\n")),
            params=params,
        )

        if method == "tools/call":
            pending.tool_name = str(params.get("name") or "unknown")
            pending.arguments = params.get("arguments", {})
            call_id = self._call_id(key)
            with self._lock:
                self.stats.tool_calls += 1
                self.stats.call_input_tokens += token_estimate(pending.arguments)
            self.writer.write(
                {
                    "schema": SCHEMA,
                    "event": "tool_call",
                    "timestamp": pending.started_wall,
                    "session": self.session_id,
                    "call_id": call_id,
                    "server": self.server_name,
                    "tool": pending.tool_name,
                    "arguments": pending.arguments,
                    "transport": "stdio",
                    "request_bytes": pending.request_bytes,
                }
            )

        with self._lock:
            self.pending[key] = pending
            self.stats.request_bytes += pending.request_bytes

    def observe_server(self, raw: bytes) -> None:
        with self._lock:
            self.stats.frames_from_server += 1
        msg = self._decode(raw)
        if msg is None or "id" not in msg or "method" in msg:
            return

        key = jsonrpc_key(msg.get("id"))
        with self._lock:
            pending = self.pending.pop(key, None)
        if pending is None:
            return

        response_bytes = len(raw.rstrip(b"\r\n"))
        elapsed_ms = max(0, int((time.monotonic() - pending.started_monotonic) * 1000))
        with self._lock:
            self.stats.response_bytes += response_bytes

        if pending.method == "tools/list":
            self._record_tool_schema_snapshot(pending, msg, response_bytes, elapsed_ms)
        elif pending.method == "tools/call":
            self._record_tool_result(key, pending, msg, response_bytes, elapsed_ms)

    def _record_tool_schema_snapshot(
        self,
        pending: PendingRequest,
        msg: dict[str, Any],
        response_bytes: int,
        latency_ms: int,
    ) -> None:
        result = msg.get("result") if isinstance(msg.get("result"), dict) else {}
        tools = result.get("tools") if isinstance(result.get("tools"), list) else []
        definitions = [tool for tool in tools if isinstance(tool, dict)]
        snapshot_bytes = len(compact_json(definitions).encode("utf-8"))
        snapshot_tokens = token_estimate(definitions)

        with self._lock:
            for tool in definitions:
                name = tool.get("name")
                key = f"name:{name}" if isinstance(name, str) and name else f"hash:{sha256_text(tool)}"
                self._tools_by_key[key] = tool
            catalog = [self._tools_by_key[key] for key in sorted(self._tools_by_key)]
            catalog_bytes = len(compact_json(catalog).encode("utf-8"))
            catalog_tokens = token_estimate(catalog)
            self.stats.schema_snapshots += 1
            self.stats.schema_bytes = catalog_bytes
            self.stats.schema_tokens = catalog_tokens
            self.stats.tool_count = len(catalog)

        cursor = pending.params.get("cursor") if isinstance(pending.params, dict) else None
        self.writer.write(
            {
                "schema": SCHEMA,
                "event": "tool_schema_snapshot",
                "timestamp": iso_now(),
                "session": self.session_id,
                "server": self.server_name,
                "transport": "stdio",
                "cursor": cursor,
                "next_cursor": result.get("nextCursor"),
                "snapshot_tool_count": len(definitions),
                "snapshot_schema_bytes": snapshot_bytes,
                "snapshot_estimated_schema_tokens": snapshot_tokens,
                "tool_count": len(catalog),
                "schema_bytes": catalog_bytes,
                "estimated_schema_tokens": catalog_tokens,
                "response_bytes": response_bytes,
                "latency_ms": latency_ms,
                "tools": definitions,
            }
        )

    def _record_tool_result(
        self,
        key: str,
        pending: PendingRequest,
        msg: dict[str, Any],
        response_bytes: int,
        latency_ms: int,
    ) -> None:
        protocol_error = "error" in msg and msg.get("error") is not None
        result = msg.get("result")
        tool_error = protocol_error or (isinstance(result, dict) and bool(result.get("isError", False)))
        payload = msg.get("error") if protocol_error else result
        output_tokens = token_estimate(payload)
        with self._lock:
            self.stats.tool_errors += int(tool_error)
            self.stats.call_output_tokens += output_tokens
            self.stats.latencies_ms.append(latency_ms)
        self.writer.write(
            {
                "schema": SCHEMA,
                "event": "tool_result",
                "timestamp": iso_now(),
                "session": self.session_id,
                "call_id": self._call_id(key),
                "server": self.server_name,
                "tool": pending.tool_name or "unknown",
                "result": payload,
                "error": tool_error,
                "success": not tool_error,
                "transport": "stdio",
                "response_bytes": response_bytes,
                "latency_ms": latency_ms,
            }
        )

    def finish(self, exit_code: int | None) -> None:
        self.writer.write(
            {
                "schema": SCHEMA,
                "event": "capture_end",
                "timestamp": iso_now(),
                "session": self.session_id,
                "server": self.server_name,
                "transport": "stdio",
                "exit_code": exit_code,
                "tool_calls": self.stats.tool_calls,
                "tool_errors": self.stats.tool_errors,
                "schema_tokens": self.stats.schema_tokens,
                "schema_bytes": self.stats.schema_bytes,
                "call_input_tokens": self.stats.call_input_tokens,
                "call_output_tokens": self.stats.call_output_tokens,
                "request_bytes": self.stats.request_bytes,
                "response_bytes": self.stats.response_bytes,
                "malformed_frames": self.stats.malformed_frames,
            }
        )

    @staticmethod
    def _call_id(key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
        return f"mcp-{digest}"


def copy_stderr(source: BinaryIO, target: BinaryIO) -> None:
    while True:
        chunk = source.read(8192)
        if not chunk:
            break
        target.write(chunk)
        target.flush()


def pump_client_to_server(session: CaptureSession, child_stdin: BinaryIO) -> None:
    try:
        while True:
            raw = sys.stdin.buffer.readline()
            if not raw:
                break
            session.observe_client(raw)
            child_stdin.write(raw)
            child_stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            child_stdin.close()
        except OSError:
            pass


def default_trace_path(server_name: str) -> pathlib.Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in server_name).strip("-") or "mcp"
    return pathlib.Path.home() / ".tooltax" / "traces" / f"{safe}-{stamp}.jsonl"


def parse_env(values: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in values:
        if "=" not in item:
            raise ValueError(f"--env expects KEY=VALUE, got: {item!r}")
        key, value = item.split("=", 1)
        if not key:
            raise ValueError("--env key cannot be empty")
        out[key] = value
    return out


def format_summary(session: CaptureSession, trace_path: pathlib.Path, exit_code: int | None) -> str:
    s = session.stats
    avg_latency = round(sum(s.latencies_ms) / len(s.latencies_ms)) if s.latencies_ms else 0
    return (
        "ToolTax capture complete\n"
        f"  server: {session.server_name}\n"
        f"  calls: {s.tool_calls} ({s.tool_errors} errors)\n"
        f"  tool schemas: {s.tool_count} tools, ~{s.schema_tokens:,} tokens ({s.schema_bytes:,} bytes)\n"
        f"  observed call payload: ~{s.call_input_tokens + s.call_output_tokens:,} tokens\n"
        f"  average tool latency: {avg_latency:,} ms\n"
        f"  malformed protocol frames observed: {s.malformed_frames}\n"
        f"  child exit: {exit_code}\n"
        f"  trace: {trace_path}"
    )


def run_proxy(args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("missing MCP server command; use: tool_tax_capture.py [options] -- <command> [args...]")

    trace_path = pathlib.Path(os.path.expanduser(args.trace)) if args.trace else default_trace_path(args.server_name)
    writer = TraceWriter(trace_path)
    session = CaptureSession(args.server_name, writer)

    env = os.environ.copy()
    try:
        env.update(parse_env(args.env))
    except ValueError as exc:
        writer.close()
        raise SystemExit(str(exc)) from exc

    try:
        child = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=args.cwd or None,
            env=env,
            bufsize=0,
        )
    except OSError as exc:
        session.finish(None)
        writer.close()
        raise SystemExit(f"failed to launch MCP server: {exc}") from exc
    assert child.stdin is not None
    assert child.stdout is not None
    assert child.stderr is not None

    def forward_signal(signum: int, _frame: Any) -> None:
        try:
            child.send_signal(signum)
        except ProcessLookupError:
            pass

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, forward_signal)
        except (ValueError, OSError):
            pass

    stdin_thread = threading.Thread(
        target=pump_client_to_server,
        args=(session, child.stdin),
        name="tooltax-client-to-server",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=copy_stderr,
        args=(child.stderr, sys.stderr.buffer),
        name="tooltax-server-stderr",
        daemon=True,
    )
    stdin_thread.start()
    stderr_thread.start()

    try:
        while True:
            raw = child.stdout.readline()
            if not raw:
                break
            session.observe_server(raw)
            sys.stdout.buffer.write(raw)
            sys.stdout.buffer.flush()
    except BrokenPipeError:
        try:
            child.terminate()
        except ProcessLookupError:
            pass

    exit_code = child.wait()
    stdin_thread.join(timeout=1)
    stderr_thread.join(timeout=1)
    session.finish(exit_code)
    writer.close()

    if not args.quiet:
        print(format_summary(session, trace_path, exit_code), file=sys.stderr)
    return int(exit_code or 0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture live stdio MCP traffic and write ToolTax JSONL without changing the protocol stream."
    )
    parser.add_argument("--server-name", required=True, help="Stable name used to group this MCP server in ToolTax reports.")
    parser.add_argument("--trace", help="Output JSONL path. Default: ~/.tooltax/traces/<server>-<timestamp>.jsonl")
    parser.add_argument("--cwd", help="Working directory for the wrapped MCP server.")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="Environment override for the wrapped server; repeatable.")
    parser.add_argument("--quiet", action="store_true", help="Suppress the capture summary on stderr.")
    parser.add_argument("--version", action="version", version=f"ToolTax capture {VERSION}")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="MCP server command, normally after --.")
    return parser


def main() -> int:
    return run_proxy(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
