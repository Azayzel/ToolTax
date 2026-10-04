import importlib.util
import pathlib
import sys
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


if __name__ == "__main__":
    unittest.main()
