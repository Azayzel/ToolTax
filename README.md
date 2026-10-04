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

Vendor transcript formats are treated as adapters, not stable contracts. The normalized format is documented in [`references/TRACE_FORMAT.md`](references/TRACE_FORMAT.md).

## Honest metric semantics

The core metric is **observed payload tokens**, estimated as roughly four UTF-8 characters per token across tool arguments and results. It is deliberately *not* labeled API spend.

Where model usage counters exist in a trace, ToolTax surfaces them separately. It does not assume the complete tool-schema/context cost is recoverable from local transcripts.

## Scoring

The utility score is a transparent heuristic, not a claim about business ROI. See [`references/SCORING.md`](references/SCORING.md).

## Design principles

1. Local-first: traces can contain source code, prompts, credentials, and private data.
2. Zero dependencies for the core analyzer.
3. Adapter isolation: vendor format churn should not infect scoring logic.
4. Conservative claims: distinguish measured values from estimates.
5. Useful before fancy: CLI first; dashboards later.

## Roadmap

- live MCP capture/proxy layer
- tool-schema context tax measurement
- before/after optimization comparison
- GitHub Action regression checks
- additional agent adapters
- richer overlap/duplicate-capability detection

See [`PLAN.md`](PLAN.md), [`DECISIONS.md`](DECISIONS.md), and [`SHELVED.md`](SHELVED.md).

## Development

```bash
python -m unittest discover -s tests -v
```

## Privacy

ToolTax processes traces locally and performs no network calls. Treat session files as sensitive. Do not publish raw traces without reviewing them for secrets and private data.

## License

MIT
