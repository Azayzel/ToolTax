# ToolTax

**Find out which AI tools are earning their keep.**

ToolTax audits agent/MCP usage for duplicate calls, failures, empty results, oversized outputs, latency, and low-utility servers. It can analyze existing Claude Code/Codex traces or capture stdio MCP traffic live.

```text
TOOLTAX — AGENT TOOL COST REPORT
====================================================================
Calls: 10   Estimated payload: ~35,xxx tok   Potential excess/signals: ...

SERVER / BUILTIN TOOL  CALLS     TOKENS  SIGNALS   SCORE  RECOMMENDATION
----------------------------------------------------------------------------------
context7                   2     ...       ...%    ...   review large text outputs
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

ToolTax does **not** claim to reconstruct provider billing. Advertised schema tokens and payload tokens use deterministic, provider-neutral estimates so runs can be compared consistently. Live capture preserves the original payload estimates even when the stored trace is redacted.

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

With no path, ToolTax discovers local **clients first**, even when they have no supported transcripts:

```bash
python scripts/tool_tax.py
python scripts/tool_tax.py --share-safe --format json
```

## Terminal experience

Interactive text runs show an animated scanning indicator with phase updates for client discovery, trace parsing, scoring, and customization auditing. The report uses a cyan heading, colored scores, and compact score bars on narrower terminals. Animation runs only while work is happening; there are no artificial delays.

```bash
python scripts/tool_tax.py
python scripts/tool_tax.py --demo
python scripts/tool_tax.py --plain
```

`--plain` disables animation, colors, and compact styling. `NO_COLOR` disables colors while retaining progress. CI, `TERM=dumb`, redirected streams, JSON, Markdown, and file exports automatically disable terminal effects. Progress goes to stderr and is cleared before the report is printed.

## Local discovery

The default report starts with client roots, configuration, session/history evidence, configured models, and MCP definitions. It then correlates supported calls as **MODEL -> CLIENT -> MCP SERVER -> TOOL**. A client with only memory files or session metadata is still reported; it is no longer hidden behind `Files: 0 / Sources: none`.

| Client | Locations and evidence |
| --- | --- |
| Claude Code / Claude VS Code | `~/.claude`, `~/.claude.json`, sessions, projects, IDE state, plugin definitions, workspace `.mcp.json` and Claude settings |
| Codex | `~/.codex`, TOML config, sessions and archived sessions, observed turn models and usage |
| Cursor | `~/.cursor`, editor user settings, global/workspace storage, profiles, MCP config and history candidates |
| VS Code / Copilot | `~/.copilot`, Code/Insiders/VSCodium user storage, profiles, chat state, local VS Code Server data, workspace MCP config |
| Continue | `~/.continue`, workspace config, JSON/YAML model and MCP settings, session files and extension storage |
| Additional clients | Gemini CLI, Windsurf, Cline, Roo Code, Kiro, OpenCode, Amp and Goose roots; recognized MCP configs; Claude Desktop config |

The named client registry follows the detection approach used by [Vercel's Skills CLI](https://github.com/vercel-labs/skills/blob/main/src/agents.ts). Skills installation directories are evidence of client presence, **not** proof of MCP usage. ToolTax maintains its own registry and does not run `npx`, download registries, or contact servers during discovery.

Discovery checks Windows roaming application data, macOS Application Support, and XDG config locations. It honors `APPDATA`, `XDG_CONFIG_HOME`, `CLAUDE_CONFIG_DIR`, and `CODEX_HOME`. Project configuration is checked in the current working directory; unrelated repositories and the whole disk are not recursively searched. Editor profiles, workspace storage, and global storage have separate scan budgets. `--source claude` or `--source codex` limits default discovery to that client.

JSON and JSONC configuration are supported without dependencies. TOML uses Python 3.11+'s standard library; Python 3.10 can use `tomli`. YAML configuration uses optional `PyYAML`:

```bash
python -m pip install PyYAML "tomli; python_version < '3.11'"
```

Missing optional parsers produce an inventory warning, not a crash. Core trace analysis remains dependency-free. Recognized MCP definitions include stdio, HTTP/streamable HTTP, and SSE transports. Unknown transports and disabled definitions remain labeled; imported/remote configs and package references are not resolved, and installed plugin definitions are not proof that a plugin is enabled.

**Evidence and limits:**

- Configured models are separate from models observed on tool calls. Configured servers do not imply calls, advertised tool schemas, or a model/server association; unknown links stay unknown.
- Supported Claude, Codex, and canonical JSONL traces are scored. JSON exports containing those same records, either as arrays or under `records`/`messages`, are also recognized.
- VS Code/Copilot saved JSON chat sessions and JSONL mutation journals are decoded locally. Recorded `toolCallRounds` and `toolCallResults` supply paired calls and payloads; request `modelId` and serialized MCP server labels supply attribution. Journals are replayed before analysis so updates are not counted as extra calls. Default discovery selects the newest readable saved copy of each session (JSONL wins a modification-time tie). Copies remain visible as `superseded-session-copy`; duplicate-call findings stay within a client/session.
- Copilot invocation display messages alone are not treated as tool payloads. Missing results, model IDs, provider usage, and per-call latency are not fabricated. A selected `auto` model remains `auto`, not an inferred backend model. Malformed journals are reported and excluded. Copilot CLI, proprietary Cursor/Continue histories, and SQLite databases remain inventory-only unless they contain an already supported format.
- Scans are read-only, skip symlinks, and stop at 10,000 files per root. Configs over 4 MiB and automatically discovered history over 64 MiB are not parsed. Warnings/history statuses identify incomplete results; explicit trace paths bypass the automatic history size limit.
- Reports omit server commands, arguments, environment values, headers, endpoint URLs, and raw session content. `--share-safe` also suppresses inventory and warning paths. Client/model/server/tool identifiers remain visible; review reports before sharing.

Text and Markdown provide a compact inventory and correlation tree. JSON includes full `discovery.clients`, `discovery.warnings`, and evidence-labeled `correlations`. The cost report's `files_scanned` counts successfully read, recognized traces during discovery, not config files, opaque databases, or metadata-only files. Explicit paths, demo mode, and before/after inputs do not trigger client discovery.

## Skills and instructions audit

Default discovery also performs a separate read-only customization audit. It checks the current workspace, shared/user skill directories, named client customization folders, standard editor prompts/profiles, and known extension skill/agent locations. Recognized files include `SKILL.md`, `AGENTS.md`, `CLAUDE.md`, `GEMINI.md`, `copilot-instructions.md`, `*.instructions.md`, `*.agent.md`, `*.prompt.md`, and client rule files. Imports, remote packages, arbitrary configured locations, and unrelated repositories are not followed.

```bash
python scripts/tool_tax.py --no-customizations
python scripts/tool_tax.py ./sessions --audit-customizations --format json --share-safe
```

The audit reports static file size, identical-content groups, repeated paragraphs of at least 120 characters, files above 2,000 estimated tokens, declared broad scopes, and simple opposing `always`/`never` rules. Scope/frontmatter parsing uses optional PyYAML; missing parsers remain visible in `frontmatter_status`. These are review candidates, not proof of unnecessary context or semantic contradictions.

Evidence stays separate:

- **Installed:** a file exists in a scanned location. Extension files may not be enabled.
- **Declared scope:** metadata says broad, scoped, or on-demand; actual applicability remains unverified.
- **Observed file read:** a successful supported built-in read used that exact absolute path. Relative paths, shell commands, and inferred reads are not matched. This does not prove prompt injection, full-file loading, or instruction compliance.
- **Tool mention:** the current file names a tool seen in the traces. This is not evidence that the instruction caused those calls; historical file contents may differ.

Static tokens are **never added to usage or waste totals**. JSON exposes all records under `customizations`, including evidence and file/line references for opposing rules. Text and Markdown show a compact summary. Reports do not include file bodies; `--share-safe` suppresses paths. Scans skip symlinks and dependency/build folders, visit at most 10,000 files per root, and read at most 4 MiB per customization and 32 MiB total. Statuses and warnings identify incomplete coverage.

## Reading the scores

Built-in tools have individual rows and recommendations instead of one blanket `builtin` recommendation. JSON retains aggregate `servers` metrics for existing consumers and adds `builtin_tools`.

Copilot typed media attachments are excluded from text token estimates and tracked separately. Image-only results are not treated as empty. JSON reports `media_attachments`, `media_encoded_chars` (stored encoded characters, not decoded bytes), and `media_token_cost: null`. Unknown media cost is not zero. Other adapters and preserved live-capture estimates retain their existing accounting; media embedded in ordinary text cannot be reliably separated.

Output above a threshold is **potential excess**, not proven waste. Human-readable reports use review-signal language. Existing JSON `waste_*`/`estimated_waste_*` keys, finding identifiers, and budget semantics remain for compatibility; the heuristic score is not semantic usefulness or ROI. See [the scoring reference](references/SCORING.md).

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

## Before / after

Capture or collect a baseline and an optimized run, then compare them directly:

```bash
python scripts/tool_tax.py \
  --before ~/.tooltax/traces/before.jsonl \
  --after ~/.tooltax/traces/after.jsonl
```

The comparison reports advertised schema size, observed tool payload, estimated waste, call count, errors, duplicate calls, and median latency with percentage deltas. JSON and Markdown output work here too.

```bash
python scripts/tool_tax.py --before before.jsonl --after after.jsonl --format markdown -o savings.md
```

Use representative runs for both sides. ToolTax measures change in the observed runs; it does not claim causality from an uncontrolled before/after experiment.

## Share-safe redaction

Capture with redaction enabled:

```bash
python scripts/tool_tax_capture.py \
  --server-name my-server \
  --redact \
  --trace ~/.tooltax/traces/my-server.safe.jsonl \
  -- <server-command> <server-args...>
```

Or sanitize an existing JSONL trace into a separate file:

```bash
python scripts/tool_tax_redact.py raw.jsonl share-safe.jsonl
```

Redaction removes values under common secret-bearing keys plus common email/token/home-path patterns. It is a defensive sharing aid, **not a formal DLP guarantee**. The redactor refuses to overwrite the source and drops malformed raw lines instead of copying them into a supposedly safe file.

For aggregate reports, `--share-safe` suppresses local source-file paths:

```bash
python scripts/tool_tax.py share-safe.jsonl --share-safe --format markdown
```

See [`references/REDACTION.md`](references/REDACTION.md) and [`references/COMPARISON.md`](references/COMPARISON.md).

## CI budget gate

Turn ToolTax into a PR guardrail with the composite GitHub Action:

```yaml
- uses: actions/checkout@v7
- uses: Azayzel/ToolTax@v0
  with:
    trace-path: .tooltax/current.jsonl
    max-waste-percent: '20'
    max-schema-tokens: '10000'
    max-duplicate-calls: '5'
    max-errors: '2'
```

Configured budgets fail closed when required telemetry is missing, so a schema budget cannot silently pass on a trace that never captured schemas. The action writes `tooltax-budget.md`, adds a job summary, exposes metric outputs, and fails the step on violations.

The same gate works locally with `scripts/tool_tax_budget.py`. See [`references/CI_BUDGET.md`](references/CI_BUDGET.md).
## Agent skill

The repository root is a portable Agent Skill. Invoke it as **`/tool-tax`** in clients that expose skill commands, or ask your agent to audit tool/MCP waste.

## Supported inputs

- ToolTax canonical JSONL (`tooltax.v1`)
- ToolTax stdio live-capture JSONL
- Claude Code transcript JSONL — best effort
- OpenAI Codex rollout JSONL — best effort
- VS Code/Copilot saved chat JSON and JSONL mutation journals — best effort, recorded tool-call metadata required
- JSON arrays or `records`/`messages` wrappers containing the supported records above

Vendor session formats change. ToolTax isolates those differences in adapters and owns a stable normalized trace format for integrations.

## Privacy

Tool traces can contain source code, command output, credentials, customer data and prompts. Live capture also records tool arguments/results and advertised tool definitions. ToolTax is local-first and performs no network calls of its own. Prefer `--redact` at capture time or create a separate redacted copy before sharing; publish aggregate reports when possible.

## Tests

```bash
python -m unittest discover -s tests -v
```

The capture tests launch a fake stdio MCP server and verify that protocol traffic is unchanged while schema/call telemetry is recorded.

## Status

**v0.3.0** — live stdio capture, redaction, before/after comparison, and configurable GitHub Actions CI budgets are implemented. Next team features are trend reports, annotations, and aggregate rollups.

## License

MIT
