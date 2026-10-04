# Decisions

## Local-first, no telemetry
Raw agent traces are sensitive. ToolTax analyzes and captures locally and does not transmit anything itself.

## Python standard library only
The skill should run on macOS, Linux, and Windows without an install step beyond Python 3.10+.

## Own a canonical trace model
Vendor transcript formats are implementation details and can change. Parsing is adapter-specific; scoring consumes normalized `ToolCall` records.

## Do not call estimated payload "cost"
Characters/4 is useful for comparable magnitude, not billing. Reports say observed/estimated payload tokens.

## Utility, not ROI
A transcript cannot prove business value. The score only reflects observable execution quality/waste signals.

## Conservative duplicate accounting
For repeated identical calls, only repeated *input* tokens are counted as definite waste. A repeated call can legitimately return a changed result.

## Unmatched calls are low severity
Missing results can mean cancellation, approval wait, or transcript persistence problems—not necessarily a broken tool.

## Live capture is a transparent stdio shim
The capture process does not implement MCP semantics. It forwards newline-delimited JSON-RPC frames unchanged between the host and child server, so it does not depend on either the legacy initialize lifecycle or the newer stateless lifecycle.

## Stdout is protocol-only
The proxy never logs to stdout. Child stderr and ToolTax diagnostics go to stderr; telemetry goes to the local JSONL trace. A single debug line on stdout would corrupt the MCP transport.

## Schema overhead is an estimate, not provider context billing
Live capture measures the exact serialized definitions returned by `tools/list`, then reports a deterministic chars/4 token estimate. Provider wrappers can transform those definitions before model context, so ToolTax labels this **advertised schema** rather than exact billed context.

## Schema catalogs de-duplicate pagination and refreshes
Repeated `tools/list` calls should not multiply schema overhead. ToolTax keeps the latest definition per tool name and unions paginated pages into one observed catalog for the session.
