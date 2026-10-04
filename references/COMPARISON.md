# Before / after comparison

ToolTax can compare representative trace sets before and after a configuration or implementation change.

```bash
python scripts/tool_tax.py \
  --before traces/baseline/ \
  --after traces/optimized/
```

`--before` and `--after` are repeatable and each value can be a JSONL file or directory.

## Metrics

The comparison reports:

- advertised MCP schema estimate
- observed tool payload
- estimated waste
- tool-call count
- error count
- duplicate-call count
- median measured/trace-derived latency
- per-server payload and waste changes

## Interpretation

Lower is better for the comparison metrics above, but the result is observational. ToolTax does not prove that a specific tuning change caused the delta.

For a meaningful comparison:

1. use the same agent/model configuration where possible
2. use the same or comparable task/workload
3. capture enough calls that one outlier does not dominate
4. avoid comparing a cold-start baseline to a warmed-cache optimized run unless cache warmup is the thing being tested
5. keep output-bloat and duplicate-window thresholds constant

Advertised schema is the de-duplicated `tools/list` definition size observed by the MCP proxy. Providers can transform those definitions before model context, so the value is not exact billed context.
