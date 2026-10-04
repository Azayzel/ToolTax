import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
REDACT = ROOT / "scripts" / "tool_tax_redact.py"
CAPTURE = ROOT / "scripts" / "tool_tax_capture.py"
FAKE = ROOT / "tests" / "fixtures" / "fake_mcp_server.py"

spec = importlib.util.spec_from_file_location("tool_tax_redaction", ROOT / "scripts" / "tool_tax_redaction.py")
redaction = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = redaction
assert spec.loader is not None
spec.loader.exec_module(redaction)


class RedactionTests(unittest.TestCase):
    def test_recursive_redaction_removes_common_secrets_and_identity(self):
        r = redaction.Redactor()
        value = {
            "authorization": "Bearer super-secret-value-123456789",
            "email": "josh@example.com",
            "path": "/Users/josh/code/private",
            "nested": {"api_key": "sk-abcdefghijklmnop123456"},
        }
        out = r.redact(value)
        text = json.dumps(out)
        self.assertNotIn("josh@example.com", text)
        self.assertNotIn("super-secret", text)
        self.assertNotIn("abcdefghijklmnop", text)
        self.assertNotIn("/Users/josh/", text)
        self.assertIn("<redacted:", text)

    def test_redact_cli_skips_malformed_raw_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = pathlib.Path(tmp) / "raw.jsonl"
            dst = pathlib.Path(tmp) / "safe.jsonl"
            src.write_text(
                json.dumps({"event": "tool_call", "arguments": {"token": "ghp_abcdefghijklmnopqrstuvwxyz123456"}})
                + "\nTHIS IS RAW SECRET junk@example.com\n",
                encoding="utf-8",
            )
            proc = subprocess.run([sys.executable, str(REDACT), str(src), str(dst)], capture_output=True, text=True, timeout=10)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            content = dst.read_text(encoding="utf-8")
            self.assertNotIn("ghp_", content)
            self.assertNotIn("junk@example.com", content)
            self.assertEqual(len(content.splitlines()), 1)

    def test_capture_redaction_preserves_original_metric_estimates(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = pathlib.Path(tmp) / "capture.jsonl"
            proc = subprocess.Popen(
                [
                    sys.executable, str(CAPTURE), "--server-name", "fake", "--trace", str(trace),
                    "--redact", "--quiet", "--", sys.executable, str(FAKE),
                ],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            requests = [
                {"jsonrpc":"2.0","id":1,"method":"initialize","params":{}},
                {"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"echo","arguments":{"text":"email me at josh@example.com","authorization":"Bearer secret-secret-secret"}}},
            ]
            wire = "".join(json.dumps(x, separators=(",", ":")) + "\n" for x in requests)
            stdout, stderr = proc.communicate(wire, timeout=10)
            self.assertEqual(proc.returncode, 0, stderr)
            events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
            call = next(e for e in events if e.get("event") == "tool_call")
            self.assertIn("estimated_input_tokens", call)
            stored = json.dumps(call)
            self.assertNotIn("josh@example.com", stored)
            self.assertNotIn("secret-secret-secret", stored)
            self.assertGreater(call["estimated_input_tokens"], 0)


if __name__ == "__main__":
    unittest.main()
