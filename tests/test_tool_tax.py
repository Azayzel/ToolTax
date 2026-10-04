import importlib.util
import pathlib
import sys
import tempfile
import json
import unittest

ROOT = pathlib.Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("tool_tax", ROOT / "scripts" / "tool_tax.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class ToolTaxTests(unittest.TestCase):
    def load(self, name, source="auto"):
        stats = mod.ParseStats()
        path = ROOT / "tests" / "fixtures" / name
        calls = mod.load_file(path, source, stats)
        return calls, stats

    def test_canonical_pairs_and_duplicate(self):
        calls, stats = self.load("canonical.jsonl")
        self.assertEqual(len(calls), 3)
        findings = mod.analyze(calls)
        self.assertTrue(any(f.kind == "duplicate-call" for f in findings))
        self.assertTrue(any(f.kind == "empty-result" for f in findings))

    def test_claude_parsing(self):
        calls, stats = self.load("claude.jsonl")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].server, "github")
        self.assertEqual(calls[0].tool, "search_code")
        self.assertFalse(calls[0].error)
        self.assertTrue(calls[1].error)
        self.assertEqual(stats.model_input_tokens, 220)

    def test_codex_parsing(self):
        calls, stats = self.load("codex.jsonl")
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].server, "docs")
        self.assertEqual(calls[0].tool, "lookup_term")
        self.assertTrue(calls[1].error)

    def test_report_caps_waste(self):
        calls, stats = self.load("canonical.jsonl")
        findings = mod.analyze(calls)
        data = mod.report_data(calls, findings, stats, [])
        self.assertLessEqual(data["estimated_waste_tokens"], data["observed_tokens"])
        self.assertTrue(data["servers"])

    def test_canonical_capture_metrics_and_preserved_estimates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "capture.jsonl"
            records = [
                {"schema":"tooltax.v1","event":"tool_schema_snapshot","estimated_schema_tokens":321,"schema_bytes":1200,"tool_count":4},
                {"schema":"tooltax.v1","event":"tool_call","timestamp":"2026-10-04T10:00:00Z","call_id":"x","server":"docs","tool":"lookup","arguments":{"q":"redacted"},"estimated_input_tokens":50},
                {"schema":"tooltax.v1","event":"tool_result","timestamp":"2026-10-04T10:00:05Z","call_id":"x","server":"docs","tool":"lookup","result":"tiny","estimated_output_tokens":900,"latency_ms":1234,"success":True},
                {"schema":"tooltax.v1","event":"capture_end","schema_tokens":321,"schema_bytes":1200,"request_bytes":111,"response_bytes":222},
            ]
            path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
            stats = mod.ParseStats()
            calls = mod.load_file(path, "canonical", stats)
            self.assertEqual(calls[0].input_tokens, 50)
            self.assertEqual(calls[0].output_tokens, 900)
            self.assertEqual(calls[0].latency_ms, 1234)
            self.assertEqual(stats.advertised_schema_tokens, 321)
            self.assertEqual(stats.protocol_response_bytes, 222)

    def test_before_after_comparison(self):
        before = {
            "advertised_schema_tokens": 1000, "observed_tokens": 10000, "estimated_waste_tokens": 4000,
            "calls": 20, "errors": 4, "duplicate_calls": 5, "median_latency_ms": 800,
            "servers": [{"server":"docs","calls":20,"observed_tokens":10000,"waste_tokens":4000,"utility_score":50}],
        }
        after = {
            "advertised_schema_tokens": 600, "observed_tokens": 7000, "estimated_waste_tokens": 1000,
            "calls": 12, "errors": 1, "duplicate_calls": 1, "median_latency_ms": 500,
            "servers": [{"server":"docs","calls":12,"observed_tokens":7000,"waste_tokens":1000,"utility_score":85}],
        }
        data = mod.comparison_data(before, after)
        self.assertAlmostEqual(data["estimated_waste_reduction_pct"], 75.0)
        schema = next(m for m in data["metrics"] if m["key"] == "advertised_schema_tokens")
        self.assertAlmostEqual(schema["change_pct"], -40.0)
        self.assertEqual(data["servers"][0]["after_calls"], 12)


if __name__ == "__main__":
    unittest.main()
