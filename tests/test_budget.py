from __future__ import annotations

import argparse
import importlib.util
import pathlib
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import tool_tax  # noqa: E402
import tool_tax_budget  # noqa: E402


class BudgetTests(unittest.TestCase):
    def report(self, fixture: str) -> dict:
        args = argparse.Namespace(
            source="auto",
            duplicate_window=8,
            large_output_tokens=2000,
            share_safe=True,
        )
        return tool_tax.analyze_inputs([str(ROOT / "tests" / "fixtures" / fixture)], args)

    def test_budget_passes_within_limits(self) -> None:
        data = tool_tax_budget.evaluate_budget(
            self.report("canonical_capture.jsonl"),
            {
                "max_waste_percent": 20.0,
                "max_schema_tokens": 10000,
                "max_duplicate_calls": 0,
                "max_errors": 0,
                "max_observed_tokens": 100,
                "max_median_latency_ms": 100,
            },
        )
        self.assertTrue(data["passed"])
        self.assertEqual(data["violations_count"], 0)

    def test_budget_fails_when_schema_exceeds_limit(self) -> None:
        data = tool_tax_budget.evaluate_budget(
            self.report("canonical_capture.jsonl"),
            {"max_schema_tokens": 100},
        )
        self.assertFalse(data["passed"])
        self.assertEqual(data["violations"][0]["metric"], "advertised_schema_tokens")

    def test_missing_schema_is_failure_by_default(self) -> None:
        data = tool_tax_budget.evaluate_budget(
            self.report("canonical.jsonl"),
            {"max_schema_tokens": 10000},
        )
        self.assertFalse(data["passed"])
        self.assertIn("unavailable", data["violations"][0]["message"])

    def test_missing_schema_can_be_explicitly_allowed(self) -> None:
        data = tool_tax_budget.evaluate_budget(
            self.report("canonical.jsonl"),
            {"max_schema_tokens": 10000},
            allow_missing_metrics=True,
        )
        self.assertTrue(data["passed"])
        self.assertEqual(data["checks"][0]["status"], "skip")

    def test_cli_exit_codes(self) -> None:
        fixture = ROOT / "tests" / "fixtures" / "canonical_capture.jsonl"
        script = ROOT / "scripts" / "tool_tax_budget.py"
        passed = subprocess.run(
            [sys.executable, str(script), str(fixture), "--max-schema-tokens", "10000"],
            check=False,
            capture_output=True,
            text=True,
        )
        failed = subprocess.run(
            [sys.executable, str(script), str(fixture), "--max-schema-tokens", "100"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(passed.returncode, 0)
        self.assertEqual(failed.returncode, tool_tax_budget.EXIT_BUDGET_FAILED)
        self.assertIn("FAIL", failed.stdout)


if __name__ == "__main__":
    unittest.main()
