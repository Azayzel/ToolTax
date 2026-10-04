---
name: tool-tax
description: Audit AI agent tool and MCP usage for wasted tokens, duplicate calls, failures, empty results, oversized outputs, latency, and low-value servers. Use when the user asks where agent/MCP resources are being wasted, which tools should be disabled or tuned, why a coding agent is expensive or slow, or requests /tool-tax.
license: MIT
compatibility: Requires Python 3.10+ and local read access to agent session JSONL files. Supports ToolTax canonical JSONL, Claude Code transcripts, and OpenAI Codex rollout files on a best-effort basis.
metadata:
  version: "0.1.0"
  command: "/tool-tax"
---

# ToolTax

Measure the **tax** an agent's tools impose: duplicated calls, failed calls, empty results, oversized payloads, and servers that consume far more observed tool payload than they return in utility.

## Core rule

Never pretend ToolTax can reconstruct provider billing from a transcript. Report three things separately:

1. **Observed tool payload** — arguments + results, estimated deterministically at ~4 characters/token.
2. **Model usage present in the trace** — if the agent records it, report it as session-level context only.
3. **Estimated waste** — a conservative heuristic from directly observable failure/waste signals.

Call the score a **utility score**, not ROI. ToolTax does not know whether a successful call changed the final user outcome.

## Run

From this skill directory:

```bash
python scripts/tool_tax.py --demo
```

Audit explicit files/directories:

```bash
python scripts/tool_tax.py path/to/session.jsonl
python scripts/tool_tax.py path/to/sessions/ --format markdown -o tooltax.md
```

With no path, scan known local session locations:

```bash
python scripts/tool_tax.py
```

Current discovery targets:

- Claude Code: `~/.claude/projects/**/*.jsonl`
- OpenAI Codex: `~/.codex/sessions/**/*.jsonl`

## `/tool-tax` workflow

When invoked:

1. Identify the smallest relevant trace set. Prefer the current project/session or a user-supplied path over scanning everything.
2. Run `scripts/tool_tax.py` against those traces.
3. Present the scoreboard first: server, calls, observed payload, waste %, utility score, recommendation.
4. Then explain the top 3 concrete waste signals with evidence.
5. Recommend **one action per server**: keep, monitor, cap/filter output, deduplicate/cache, investigate reliability, or consider disabling/replacing.
6. If a trace parser finds nothing, do not guess. Explain which input formats are supported and offer the canonical schema in `references/TRACE_FORMAT.md`.

## Interpretation thresholds

Treat these as defaults, not universal truths:

- Output bloat: estimated result payload >= 2,000 tokens.
- Duplicate call: same server + tool + normalized arguments repeated within 8 tool calls.
- Empty result: a completed tool returns empty text, `null`, `[]`, or `{}`.
- Failure: explicit error signal or provider-specific error marker.
- Unmatched call: no result can be paired with the tool invocation. This can be a trace-format limitation, cancellation, approval wait, or persistence bug; keep severity low.

## Recommendation policy

- **keep**: high utility score, low observed waste.
- **monitor**: no decisive problem.
- **deduplicate/reuse results**: repeated identical calls dominate.
- **cap or filter tool output**: oversized result payloads recur.
- **investigate reliability**: errors are frequent.
- **consider disabling or replacing**: enough calls exist and both utility score and waste are poor.

Never recommend removal from one anomalous call.

## Privacy

Treat traces as sensitive. They can contain source code, shell output, secrets, customer data, file paths, and prompts. Do not upload or publish raw traces unless the user explicitly chooses to. Prefer local analysis and share aggregates.

## Adapters

The adapter layer is intentionally tolerant. Claude Code transcript JSONL is not a stable public contract, and Codex rollout records evolve over time. Keep vendor parsing isolated from scoring. Normalize new formats into the `ToolCall` model rather than adding vendor-specific scoring.

See:

- [Trace format](references/TRACE_FORMAT.md)
- [Scoring](references/SCORING.md)
