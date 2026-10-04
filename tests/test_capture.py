import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
CAPTURE = ROOT / "scripts" / "tool_tax_capture.py"
FAKE = ROOT / "tests" / "fixtures" / "fake_mcp_server.py"


class CaptureTests(unittest.TestCase):
    def test_stdio_proxy_is_protocol_transparent_and_records_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = pathlib.Path(tmp) / "capture.jsonl"
            proc = subprocess.Popen(
                [
                    sys.executable,
                    str(CAPTURE),
                    "--server-name",
                    "fake",
                    "--trace",
                    str(trace),
                    "--quiet",
                    "--",
                    sys.executable,
                    str(FAKE),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            requests = [
                {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                {"jsonrpc": "2.0", "id": 4, "method": "tools/list", "params": {"cursor": "page-2"}},
                {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "hello"}}},
            ]
            wire = "".join(json.dumps(item, separators=(",", ":")) + "\n" for item in requests)
            stdout, stderr = proc.communicate(wire, timeout=10)
            self.assertEqual(proc.returncode, 0, stderr)

            responses = [json.loads(line) for line in stdout.splitlines() if line.strip()]
            self.assertEqual([r["id"] for r in responses], [1, 2, 4, 3])
            self.assertEqual(responses[-1]["result"]["content"][0]["text"], "hello")

            events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
            names = [event.get("event") for event in events]
            self.assertIn("capture_start", names)
            self.assertIn("tool_schema_snapshot", names)
            self.assertIn("tool_call", names)
            self.assertIn("tool_result", names)
            self.assertIn("capture_end", names)

            schemas = [e for e in events if e.get("event") == "tool_schema_snapshot"]
            self.assertEqual(len(schemas), 2)
            self.assertEqual(schemas[0]["tool_count"], 1)
            self.assertEqual(schemas[1]["tool_count"], 2)
            self.assertEqual(schemas[1]["cursor"], "page-2")
            self.assertGreater(schemas[1]["estimated_schema_tokens"], schemas[0]["estimated_schema_tokens"])

            call = next(e for e in events if e.get("event") == "tool_call")
            result = next(e for e in events if e.get("event") == "tool_result")
            self.assertEqual(call["call_id"], result["call_id"])
            self.assertEqual(call["arguments"], {"text": "hello"})
            self.assertFalse(result["error"])
            self.assertGreaterEqual(result["latency_ms"], 1)

    def test_jsonrpc_id_types_do_not_collide(self):
        spec = importlib.util.spec_from_file_location("tool_tax_capture", CAPTURE)
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        self.assertNotEqual(module.jsonrpc_key(1), module.jsonrpc_key("1"))


if __name__ == "__main__":
    unittest.main()
