#!/usr/bin/env python3
import json
import sys
import time

for line in sys.stdin:
    msg = json.loads(line)
    method = msg.get("method")
    rid = msg.get("id")
    if method == "initialize":
        out = {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1.0"}}}
    elif method == "tools/list":
        cursor = msg.get("params", {}).get("cursor")
        if cursor == "page-2":
            result = {"tools": [{"name": "sum", "description": "Add values", "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}}}}]}
        else:
            result = {"tools": [{"name": "echo", "description": "Echo text", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}], "nextCursor": "page-2"}
        out = {"jsonrpc": "2.0", "id": rid, "result": result}
    elif method == "tools/call":
        time.sleep(0.01)
        args = msg.get("params", {}).get("arguments", {})
        out = {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": args.get("text", "")}], "isError": False}}
    else:
        if rid is None:
            continue
        out = {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}}
    print(json.dumps(out, separators=(",", ":")), flush=True)
