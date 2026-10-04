# ToolTax canonical trace format

ToolTax's portable ingestion format is JSONL. One JSON object per line.

`tool_call` and `tool_result` are the stable analysis events. Live capture adds optional metadata events that existing analyzers can safely ignore.

## Tool call

```json
{"schema":"tooltax.v1","event":"tool_call","timestamp":"2026-10-04T16:00:00Z","session":"abc","call_id":"1","server":"github","tool":"search_code","arguments":{"q":"TODO"}}
```

## Tool result

```json
{"schema":"tooltax.v1","event":"tool_result","timestamp":"2026-10-04T16:00:01Z","session":"abc","call_id":"1","server":"github","tool":"search_code","result":{"items":[]},"success":true,"error":false,"latency_ms":1000}
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
- `latency_ms` when measured directly

## Live capture metadata

### Capture start

```json
{"schema":"tooltax.v1","event":"capture_start","timestamp":"2026-10-04T16:00:00Z","session":"stdio-...","server":"github","transport":"stdio","capture_version":"0.2.0"}
```

### Tool schema snapshot

Emitted for a successful `tools/list` response:

```json
{"schema":"tooltax.v1","event":"tool_schema_snapshot","timestamp":"2026-10-04T16:00:01Z","session":"stdio-...","server":"github","transport":"stdio","snapshot_tool_count":12,"snapshot_schema_bytes":15420,"snapshot_estimated_schema_tokens":3855,"tool_count":12,"schema_bytes":15420,"estimated_schema_tokens":3855,"latency_ms":40,"tools":[...]}
```

The `snapshot_*` fields describe only that response page. `tool_count`, `schema_bytes`, and `estimated_schema_tokens` describe the de-duplicated catalog observed so far across pagination/refresh responses.

### Capture end

```json
{"schema":"tooltax.v1","event":"capture_end","timestamp":"2026-10-04T16:10:00Z","session":"stdio-...","server":"github","transport":"stdio","exit_code":0,"tool_calls":18,"tool_errors":1,"schema_tokens":3855}
```

## Compatibility rule

Readers must ignore unknown events and unknown fields. This lets capture telemetry evolve without breaking the stable `tool_call` / `tool_result` analysis contract.

ToolTax deliberately owns this normalized format so scoring does not depend on unstable vendor transcript schemas.
