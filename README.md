# ToolTax

**Find out which AI tools are earning their keep.**

ToolTax audits agent/MCP session traces for duplicate calls, failures, empty results, oversized outputs, and low-utility servers.

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

- calls by MCP server/tool
- argument + result payload size
- duplicate calls
- failed calls
- empty results
- oversized results
- end-to-end call latency when timestamps permit
- a transparent heuristic utility score

ToolTax does **not** claim to reconstruct provider billing or hidden tool-schema/context cost from transcript files.

## Quick start

Python 3.10+, no dependencies:

```bash
python scripts/tool_tax.py --demo
```

Analyze a trace:

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

## Agent skill

The repository root is a portable Agent Skill. Invoke it as **`/tool-tax`** in clients that expose skill commands, or ask your agent to audit tool/MCP waste.

## Supported inputs

- ToolTax canonical JSONL (`tooltax.v1`)
- Claude Code transcript JSONL — best effort
- OpenAI Codex rollout JSONL — best effort

Vendor session formats change. ToolTax isolates those differences in adapters and owns a stable normalized trace format for integrations.

## Privacy

Tool traces can contain source code, command output, credentials, customer data and prompts. ToolTax is local-first and dependency-free. Publish aggregate reports, not raw traces.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Status

v0.1.0 — deliberately small. The next useful layer is outcome-aware analysis and capture adapters that can observe tool-schema overhead and true execution timings without depending on reverse-engineered transcript files.

## License

MIT
