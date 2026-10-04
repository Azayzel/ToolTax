# Scoring model

ToolTax v0.1 uses transparent heuristics rather than pretending to calculate business ROI.

## Observed tokens

`observed_tokens = estimated_tokens(arguments) + estimated_tokens(result)`

The estimator is deterministic: approximately `ceil(characters / 4)`.

This is **not provider billing**. It measures payload magnitude consistently across tools.

## Waste signals

| Signal | Waste attribution | Severity |
|---|---:|---|
| Failed call | full observed payload | high |
| Empty result | full observed payload | medium |
| Oversized output | tokens above threshold | medium |
| Duplicate call | repeated input payload only | medium |
| Unmatched call | input payload | low |

To reduce double counting in aggregate server metrics, waste is capped at the server's observed payload total.

## Utility score

Starts at 100 and subtracts penalties for:

- error rate: up to 35 points
- waste share: up to 40 points
- duplicate-call share: up to 10 points
- empty-result share: up to 15 points

It is intentionally conservative and interpretable. A successful call can still be semantically useless; v0.1 does not claim to know that.

## Latency

Latency is invocation timestamp → result timestamp when both are present. For Claude Code this can include user approval wait, so do not describe it as pure execution time.
