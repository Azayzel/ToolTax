# ToolTax

**Find out which AI tools are earning their keep.**

ToolTax audits agent/MCP usage for duplicate calls, failures, empty results, oversized outputs, latency, and low-utility servers. It can analyze existing Claude Code/Codex traces or capture stdio MCP traffic live.

```text
TOOLTAX — AGENT TOOL COST REPORT
====================================================================
Calls: 10   Observed payload: ~35,xxx tok   Estimated waste: ...

SERVER                CALLS     TOKENS    WASTE   SCORE  RECOMMENDATION
----------------------------------------------------------------------------------
context7                   2     ...       ...%    ...   cap or filter tool output
memory                     3     ...       ...%    ...   deduplicate/reuse results
github                     4     ...       ...%    ...   keep
filesystem                 1     ...       ...%    ...   monitor
```

## Why

Adding more MCP servers can make an agent *worse*: more calls, more context, repeated lookups, larger outputs, more latency, and more failure surface. Those costs are usually invisible.

ToolTax makes the visible portion measurable.

## What it measures

From saved traces:

- calls by MCP server/tool
- argument + result payload size
- duplicate calls
- failed calls
- empty results
- oversized results
- end-to-end call latency when timestamps permit
- a transparent heuristic utility score

From live stdio capture, ToolTax additionally observes:

- exact proxy-observed call latency
- exact JSON-RPC request/response byte size
- advertised `tools/list` definitions
- de-duplicated tool schema size across paginated/refresh responses
- protocol-level and `isError` tool failures

ToolTax does **not** claim to reconstruct provider billing. Advertised schema tokens and payload tokens use a deterministic ~4 characters/token estimate so runs can be compared consistently.

## Quick start

Python 3.10+, no dependencies:

```bash
python scripts/tool_tax.py --demo
```

Analyze existing traces:

```bash
python scripts/tool_tax.py ~/.codex/sessions/2026/10/04/
python scripts/tool_tax.py ~/.claude/projects/my-project/
```

Export Markdown or JSON:

```bash
python scripts/tool_tax.py ./sessions --format markdown -o tooltax-report.md
python scripts/tool_tax.py ./sessions --format json -o tooltax-report.json
```

With no path, ToolTax searches known local Claude Code and Codex session locations.

## Live MCP capture

For a stdio MCP server, put ToolTax between the host and the real server:

```bash
python scripts/tool_tax_capture.py \
  --server-name my-server \
  --trace ~/.tooltax/traces/my-server.jsonl \
  -- <server-command> <server-args...>
```

Then analyze the captured calls with the normal auditor:

```bash
python scripts/tool_tax.py ~/.tooltax/traces/my-server.jsonl
```

A generic MCP host configuration looks like:

```json
{
  "mcpServers": {
    "my-server": {
      "command": "python",
      "args": [
        "/absolute/path/to/ToolTax/scripts/tool_tax_capture.py",
        "--server-name", "my-server",
        "--",
        "<real-server-command>",
        "<real-server-arg>"
      ]
    }
  }
}
```

The proxy forwards protocol frames byte-for-byte. ToolTax diagnostics and the end-of-run summary go to **stderr only** because stdout belongs to the MCP JSON-RPC transport.

The capture layer deliberately does not implement or alter MCP lifecycle semantics. That keeps it compatible with both legacy initialize-based sessions and newer stateless MCP revisions.

See [`references/CAPTURE.md`](references/CAPTURE.md) for capture semantics and limitations.

## Agent skill

The repository root is a portable Agent Skill. Invoke it as **`/tool-tax`** in clients that expose skill commands, or ask your agent to audit tool/MCP waste.

## Supported inputs

- ToolTax canonical JSONL (`tooltax.v1`)
- ToolTax stdio live-capture JSONL
- Claude Code transcript JSONL — best effort
- OpenAI Codex rollout JSONL — best effort

Vendor session formats change. ToolTax isolates those differences in adapters and owns a stable normalized trace format for integrations.

## Privacy

Tool traces can contain source code, command output, credentials, customer data and prompts. Live capture also records tool arguments/results and advertised tool definitions. ToolTax is local-first and performs no network calls of its own. Publish aggregate reports, not raw traces.

## Tests

```bash
python -m unittest discover -s tests -v
```

The capture tests launch a fake stdio MCP server and verify that protocol traffic is unchanged while schema/call telemetry is recorded.

## Status

**v0.2.0** — live stdio MCP capture is implemented. The next v0.2 work is filtering/date windows, a redaction pipeline for shareable traces, and before/after baseline comparison.

## License

MIT
