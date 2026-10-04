---
name: tool-tax
description: Audit AI agent tool and MCP usage for wasted tokens, duplicate calls, failures, empty results, oversized outputs, latency, tool-schema overhead, and low-value servers. Use when the user asks where agent/MCP resources are being wasted, which tools should be disabled or tuned, why a coding agent is expensive or slow, requests live MCP capture, or requests /tool-tax.
license: MIT
compatibility: Requires Python 3.10+. Supports local ToolTax JSONL, Claude Code transcripts, OpenAI Codex rollouts, and live stdio MCP capture on a best-effort/transport-transparent basis.
metadata:
  version: "0.2.0"
  command: "/tool-tax"
---

# ToolTax

Measure the **tax** an agent's tools impose: duplicated calls, failed calls, empty results, oversized payloads, slow calls, and servers that consume far more observed tool payload than they return in utility.

## Core rule

Never pretend ToolTax can reconstruct provider billing from a transcript. Report these separately:

1. **Observed tool payload** — arguments + results, estimated deterministically at ~4 characters/token.
2. **Advertised tool schema** — definitions observed from live `tools/list`; useful for relative context-overhead comparisons, not provider billing.
3. **Model usage present in a trace** — if the agent records it, report it as session-level context only.
4. **Estimated waste** — a conservative heuristic from directly observable failure/waste signals.

Call the score a **utility score**, not ROI. ToolTax does not know whether a successful call changed the final user outcome.

## Analyze traces

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

## Capture live stdio MCP traffic

Wrap the real MCP server command:

```bash
python scripts/tool_tax_capture.py \
  --server-name my-server \
  --trace ~/.tooltax/traces/my-server.jsonl \
  -- <real-server-command> <args...>
```

The wrapper must remain protocol-transparent:

- forward stdin -> child stdin unchanged
- forward child stdout -> stdout unchanged
- mirror child stderr -> stderr
- write ToolTax telemetry to the trace file only
- write ToolTax diagnostics to stderr only, never stdout
- do not inject an initialize handshake or assume a lifecycle revision

Live capture records `tool_call` / `tool_result` events compatible with `scripts/tool_tax.py`, plus schema/capture metadata described in `references/TRACE_FORMAT.md`.

## `/tool-tax` workflow

When invoked:

1. Identify the smallest relevant trace set. Prefer the current project/session or a user-supplied path over scanning everything.
2. If usable saved traces exist, run `scripts/tool_tax.py` against them.
3. If the user specifically needs live MCP measurement or schema/timing data unavailable in transcripts, configure `scripts/tool_tax_capture.py` around the target stdio server and capture a representative session.
4. Present the scoreboard first: server, calls, observed payload, waste %, utility score, recommendation.
5. For live traces, also report the advertised schema estimate and proxy-observed latency, explicitly labeling both.
6. Explain the top 3 concrete waste signals with evidence.
7. Recommend **one action per server**: keep, monitor, cap/filter output, deduplicate/cache, investigate reliability, or consider disabling/replacing.
8. If a parser finds nothing, do not guess. Explain which input formats are supported and offer the canonical schema in `references/TRACE_FORMAT.md`.

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

Treat traces as sensitive. They can contain source code, shell output, secrets, customer data, file paths, prompts, tool arguments and tool results. Do not upload or publish raw traces unless the user explicitly chooses to. Prefer local analysis and share aggregates.

## Adapters

The adapter layer is intentionally tolerant. Claude Code transcript JSONL is not a stable public contract, and Codex rollout records evolve over time. Keep vendor parsing isolated from scoring. Normalize new formats into the `ToolCall` model rather than adding vendor-specific scoring.

See:

- [Capture](references/CAPTURE.md)
- [Trace format](references/TRACE_FORMAT.md)
- [Scoring](references/SCORING.md)
