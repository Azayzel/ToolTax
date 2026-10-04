# ToolTax canonical trace format

ToolTax's portable ingestion format is JSONL. One JSON object per line.

## Tool call

```json
{"schema":"tooltax.v1","event":"tool_call","timestamp":"2026-10-04T16:00:00Z","session":"abc","call_id":"1","server":"github","tool":"search_code","arguments":{"q":"TODO"}}
```

## Tool result

```json
{"schema":"tooltax.v1","event":"tool_result","timestamp":"2026-10-04T16:00:01Z","session":"abc","call_id":"1","result":{"items":[]},"success":true,"error":false}
```

Required for useful analysis:

- `event`
- `call_id`
- `tool` on `tool_call`

Recommended:

- `timestamp`
- `session`
- `server`
- `arguments`
- `result`
- `success` or `error`

ToolTax deliberately owns this normalized format so scoring does not depend on unstable vendor transcript schemas.
