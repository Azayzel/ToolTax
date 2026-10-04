# Live MCP capture

`tool_tax_capture.py` is a local stdio transport shim. Configure the MCP host to launch ToolTax, and ToolTax launches the real MCP server as a child process.

```text
MCP host stdin  -> ToolTax -> child MCP stdin
MCP host stdout <- ToolTax <- child MCP stdout
                         \
                          -> local ToolTax JSONL
```

## Invariants

- Protocol bytes are forwarded unchanged.
- ToolTax does not add, remove, reorder, or answer MCP messages.
- ToolTax does not initiate `initialize` or other lifecycle methods.
- Child stderr is mirrored to ToolTax stderr.
- ToolTax diagnostics use stderr only.
- ToolTax telemetry never appears on protocol stdout.

These invariants make the shim lifecycle-agnostic and allow it to observe legacy initialize-based sessions and modern stateless sessions.

## Run

```bash
python scripts/tool_tax_capture.py \
  --server-name filesystem \
  --trace ~/.tooltax/traces/filesystem.jsonl \
  -- npx -y <your-mcp-server-package> <args...>
```

Optional flags:

- `--cwd PATH` — child working directory
- `--env KEY=VALUE` — child environment override; repeatable
- `--quiet` — suppress the final stderr summary
- `--trace PATH` — explicit trace destination

If `--trace` is omitted, traces are written beneath `~/.tooltax/traces/`.

## What is measured exactly

- JSON-RPC request bytes as observed by the proxy
- JSON-RPC response bytes as observed by the proxy
- elapsed wall time between a `tools/call` request and matching response
- protocol-level JSON-RPC errors
- tool-level `result.isError`
- exact serialized tool definitions returned by `tools/list`

## What is estimated

ToolTax converts captured arguments, results, and advertised tool schemas to an approximate token magnitude using ~4 characters/token. This is deliberately provider-neutral and is intended for relative comparisons, not billing reconstruction.

## Pagination and refreshes

`tools/list` may be paginated or repeated. ToolTax records each snapshot but maintains a de-duplicated session catalog keyed by tool name. A refresh therefore replaces a tool's latest observed definition instead of multiplying its schema estimate.

## Privacy

Live traces can contain secrets and customer data in tool arguments/results. Treat `~/.tooltax/traces/` as sensitive. v0.2's planned redaction pipeline is not complete yet; do not publish raw capture files.

## Current limitation

v0.2 captures **stdio** MCP servers. Streamable HTTP requires different interception rules because authorization headers, sessions, streaming, and request routing are transport semantics that a transparent proxy must preserve.
