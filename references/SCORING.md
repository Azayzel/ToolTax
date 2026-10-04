# Scoring model

ToolTax uses transparent heuristics rather than pretending to calculate business ROI.

## Observed tokens

`observed_tokens = estimated_tokens(arguments) + estimated_tokens(result)`

The estimator is deterministic: approximately `ceil(characters / 4)`.

This is **not provider billing**. It measures payload magnitude consistently across tools.

Copilot typed attachments are kept out of text estimates. Their count and stored encoded character size are reported separately; `media_token_cost` is `null` because it cannot be reconstructed from encoded size. Image-only results are not empty results. This separation currently applies to the Copilot adapter; other adapters and preserved capture estimates retain their existing accounting.

## Potential excess and review signals

The existing JSON waste fields and budget thresholds are retained for compatibility. They are heuristic signal totals, **not proven wasted tokens**. In particular, large output can be necessary for the task.

| Signal | Waste attribution | Severity |
| --- | ---: | --- |
| Failed call | full observed payload | high |
| Empty result | full observed payload | medium |
| Oversized output | tokens above threshold; usefulness unknown | medium |
| Duplicate call | repeated input payload only | medium |
| Unmatched call | input payload | low |

To reduce double counting in aggregate server metrics, waste is capped at the server's observed payload total.

## Utility score

Starts at 100 and subtracts penalties for:

- error rate: up to 35 points
- waste share: up to 40 points
- duplicate-call share: up to 10 points
- empty-result share: up to 15 points

It is interpretable, not a measurement of usefulness. A successful call can still be semantically useless; ToolTax does not claim to know that. Conversely, large useful outputs may reduce this heuristic score.

Built-in recommendations are computed per tool. The aggregate `builtin` JSON row directs readers to those individual rows. Large-output recommendations require at least two flagged calls and at least 10% of the group's calls; the advice is to review outputs, not automatically cap them. Media-only successes can have clean text signals while their media cost remains unknown.

## Static customization audit

Skills, agents, prompts, and instruction files are audited separately. File sizes, repeated paragraphs, duplicate content, declared broad scope, and simple opposing-rule candidates do not change tool usage totals or scores. Exact-path successful read calls can supply observed-read evidence, but neither installed files nor tool-name mentions prove instruction loading, compliance, or causation.

## Latency

Latency is invocation timestamp → result timestamp when both are present. For Claude Code this can include user approval wait, so do not describe it as pure execution time.
