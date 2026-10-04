# ToolTax report

**10 calls** · **~35,091 observed payload tokens** · **~31,045 estimated waste (88.5%)**

| Server | Calls | Observed tokens | Waste | Utility | Recommendation |
|---|---:|---:|---:|---:|---|
| `context7` | 2 | 35,017 | 88.5% | 65/100 | cap or filter tool output |
| `memory` | 3 | 20 | 100.0% | 47/100 | monitor |
| `filesystem` | 1 | 12 | 100.0% | 25/100 | monitor |
| `github` | 4 | 42 | 16.7% | 91/100 | keep |

## Highest-cost waste signals

- **context7/docs** — output-bloat: ~15,500 tokens (output ~17,500 tokens)
- **context7/docs** — output-bloat: ~15,500 tokens (output ~17,500 tokens)
- **filesystem/read** — error: ~12 tokens (tool call failed)
- **memory/search** — empty-result: ~7 tokens (tool returned no useful payload)
- **memory/search** — empty-result: ~7 tokens (tool returned no useful payload)
- **github/search_code** — duplicate-call: ~7 tokens (same tool + arguments repeated within 8 calls)
- **memory/search** — duplicate-call: ~6 tokens (same tool + arguments repeated within 8 calls)
- **memory/get** — unmatched-call: ~6 tokens (no matching tool result found in trace)

> Tool token counts are payload estimates, not provider billing totals. Utility is a heuristic, not causal ROI.
