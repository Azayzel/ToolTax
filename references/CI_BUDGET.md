# CI budget gate

ToolTax can fail a build when an observed agent/MCP run exceeds explicit resource budgets.

## GitHub Action

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

The action provisions Python, analyzes the trace with the same ToolTax engine used by `/tool-tax`, writes `tooltax-budget.md`, adds a GitHub job summary, and exits non-zero when a configured budget is breached.

## Available budgets

- `max-waste-percent`
- `max-schema-tokens`
- `max-duplicate-calls`
- `max-errors`
- `max-observed-tokens`
- `max-median-latency-ms`

Unconfigured budgets are ignored.

## Missing telemetry

Configured budgets fail when their underlying metric is unavailable. For example, `max-schema-tokens` requires a live capture that contains a schema snapshot. This avoids false-green builds caused by missing telemetry.

Set `allow-missing-metrics: 'true'` only when skipping unavailable metrics is intentional.

A trace with zero supported tool calls also fails by default. Set `allow-empty: 'true'` only for workflows where empty traces are valid.

## Outputs

The action exposes:

- `passed`
- `violations`
- `waste_percent`
- `schema_tokens`
- `duplicate_calls`
- `errors`
- `observed_tokens`
- `report_path`

## Local CLI

The same gate can run outside GitHub Actions:

```bash
python scripts/tool_tax_budget.py .tooltax/current.jsonl \
  --max-waste-percent 20 \
  --max-schema-tokens 10000 \
  --max-duplicate-calls 5
```

Exit codes:

- `0`: all configured budgets passed
- `2`: one or more budgets failed
- `3`: invalid ToolTax budget configuration

Budget values are measured/estimated ToolTax telemetry, not provider billing amounts.
