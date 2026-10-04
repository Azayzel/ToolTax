# Redaction

ToolTax traces can contain credentials, customer data, prompts, source code, command output, email addresses, and local paths. Redaction is designed to make accidental sharing safer without changing live protocol traffic.

## Capture-time redaction

```bash
python scripts/tool_tax_capture.py --server-name my-server --redact -- <server-command>
```

The proxy measures the original in-memory arguments/results/schema, then redacts the telemetry object immediately before writing it to disk. The MCP request/response stream is never modified.

Captured `estimated_input_tokens`, `estimated_output_tokens`, `latency_ms`, byte counts, and advertised-schema estimates therefore describe the original observed traffic even if the stored strings become short placeholders.

## Existing traces

```bash
python scripts/tool_tax_redact.py raw.jsonl share-safe.jsonl
```

The redactor:

- never overwrites the source file
- replaces values under common secret-bearing keys
- detects common email, bearer/JWT/API-token, cloud-key, and credential-in-URL patterns
- removes the username segment from common macOS/Linux/Windows home paths
- uses per-run placeholders so repeated values stay correlatable within the copy without publishing reusable hashes
- drops malformed non-JSON lines instead of copying unknown raw text into a share-safe output

## Aggregate reports

`tool_tax.py --share-safe` suppresses local source-file paths in findings. Aggregate server/tool names are retained because they are needed to make the report actionable.

## Limitations

This is **heuristic redaction, not DLP or guaranteed anonymization**. Arbitrary secrets can appear in source code, prose, binary encodings, custom identifiers, or formats ToolTax does not recognize. Review any artifact before public release. Prefer aggregate reports over raw traces whenever possible.
