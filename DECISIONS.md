# Decisions

## Local-first, no telemetry
Raw agent traces are sensitive. v0.1 analyzes files locally and does not transmit anything.

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
