#!/usr/bin/env python3
"""ToolTax CI budget gate.

Evaluates ToolTax report metrics against explicit budgets and exits non-zero when
one or more limits are exceeded. Designed for both local CLI use and the
repository's composite GitHub Action.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
from typing import Any

SCRIPT_DIR = pathlib.Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

import tool_tax  # noqa: E402

EXIT_BUDGET_FAILED = 2
EXIT_CONFIGURATION_ERROR = 3

BUDGET_SPECS = (
    ("max_waste_percent", "estimated_waste_pct", "Estimated waste", "%", "always"),
    ("max_schema_tokens", "advertised_schema_tokens", "Advertised schema", "tok", "schema"),
    ("max_duplicate_calls", "duplicate_calls", "Duplicate calls", "calls", "always"),
    ("max_errors", "errors", "Errors", "errors", "always"),
    ("max_observed_tokens", "observed_tokens", "Observed payload", "tok", "always"),
    ("max_median_latency_ms", "median_latency_ms", "Median latency", "ms", "latency"),
)


def parse_bool(value: str | bool | None, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or str(value).strip() == "":
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def parse_optional_number(value: str | None, cast: type[int] | type[float]) -> int | float | None:
    if value is None or str(value).strip() == "":
        return None
    return cast(str(value).strip())


def analysis_args(source: str) -> argparse.Namespace:
    return argparse.Namespace(
        source=source,
        duplicate_window=8,
        large_output_tokens=2000,
        share_safe=True,
    )


def analyze_paths(paths: list[str], source: str) -> dict[str, Any]:
    return tool_tax.analyze_inputs(paths, analysis_args(source))


def metric_available(report: dict[str, Any], kind: str) -> bool:
    if kind == "schema":
        return int(report.get("schema_files") or 0) > 0
    if kind == "latency":
        return report.get("median_latency_ms") is not None
    return True


def evaluate_budget(
    report: dict[str, Any],
    budgets: dict[str, int | float | None],
    *,
    allow_missing_metrics: bool = False,
    allow_empty: bool = False,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    violations: list[dict[str, Any]] = []

    if int(report.get("calls") or 0) == 0 and not allow_empty:
        violation = {
            "budget": "non_empty_trace",
            "metric": "calls",
            "label": "Supported tool calls",
            "actual": 0,
            "limit": "> 0",
            "unit": "calls",
            "status": "fail",
            "message": "No supported tool calls were found in the supplied trace path.",
        }
        checks.append(violation)
        violations.append(violation)

    for budget_name, metric_name, label, unit, availability_kind in BUDGET_SPECS:
        limit = budgets.get(budget_name)
        if limit is None:
            continue
        actual = report.get(metric_name)
        if not metric_available(report, availability_kind):
            status = "skip" if allow_missing_metrics else "fail"
            check = {
                "budget": budget_name,
                "metric": metric_name,
                "label": label,
                "actual": None,
                "limit": limit,
                "unit": unit,
                "status": status,
                "message": (
                    f"{label} is unavailable in this trace; budget skipped because allow-missing-metrics is enabled."
                    if allow_missing_metrics
                    else f"{label} is unavailable in this trace; refusing to treat missing telemetry as a passing budget."
                ),
            }
            checks.append(check)
            if status == "fail":
                violations.append(check)
            continue

        exceeded = float(actual) > float(limit)
        check = {
            "budget": budget_name,
            "metric": metric_name,
            "label": label,
            "actual": actual,
            "limit": limit,
            "unit": unit,
            "status": "fail" if exceeded else "pass",
            "message": (
                f"{label} {format_value(actual, unit)} exceeds budget {format_value(limit, unit)}."
                if exceeded
                else f"{label} {format_value(actual, unit)} is within budget {format_value(limit, unit)}."
            ),
        }
        checks.append(check)
        if exceeded:
            violations.append(check)

    return {
        "version": tool_tax.VERSION,
        "mode": "budget",
        "passed": not violations,
        "violations_count": len(violations),
        "budgets": budgets,
        "checks": checks,
        "violations": violations,
        "report": report,
    }


def format_value(value: Any, unit: str) -> str:
    if value is None:
        return "unavailable"
    if unit == "%":
        return f"{float(value):.1f}%"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f} {unit}"
    return f"{int(value):,} {unit}"


def render_text(data: dict[str, Any]) -> str:
    lines = [
        "TOOLTAX - CI BUDGET",
        "=" * 72,
        f"Result: {'PASS' if data['passed'] else 'FAIL'}   Violations: {data['violations_count']}",
        "",
    ]
    if not data["checks"]:
        lines.append("No budgets configured. Nothing to enforce.")
    for check in data["checks"]:
        marker = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[check["status"]]
        lines.append(f"[{marker}] {check['message']}")
    return "\n".join(lines) + "\n"


def render_markdown(data: dict[str, Any]) -> str:
    status = "PASS" if data["passed"] else "FAIL"
    lines = [
        "# ToolTax CI budget",
        "",
        f"**{status}** - {data['violations_count']} violation(s)",
        "",
        "| Check | Actual | Budget | Status |",
        "|---|---:|---:|---|",
    ]
    if not data["checks"]:
        lines.append("| No budgets configured | - | - | PASS |")
    for check in data["checks"]:
        actual = format_value(check["actual"], check["unit"])
        limit = check["limit"] if isinstance(check["limit"], str) else format_value(check["limit"], check["unit"])
        lines.append(f"| {check['label']} | {actual} | {limit} | **{check['status'].upper()}** |")
    report = data["report"]
    lines += [
        "",
        "## Observed run",
        "",
        f"- Tool calls: **{report.get('calls', 0):,}**",
        f"- Observed payload: **~{report.get('observed_tokens', 0):,} tok**",
        f"- Estimated waste: **{report.get('estimated_waste_pct', 0.0):.1f}%**",
        f"- Duplicate calls: **{report.get('duplicate_calls', 0):,}**",
        f"- Errors: **{report.get('errors', 0):,}**",
    ]
    if report.get("schema_files"):
        lines.append(f"- Advertised schema: **~{report.get('advertised_schema_tokens', 0):,} tok**")
    if report.get("median_latency_ms") is not None:
        lines.append(f"- Median latency: **{report['median_latency_ms']:,} ms**")
    lines += [
        "",
        "> ToolTax budgets use observed/estimated telemetry, not provider billing. Missing telemetry fails configured budgets unless explicitly allowed.",
        "",
    ]
    return "\n".join(lines)


def github_escape(message: str) -> str:
    return message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def append_file_from_env(env_name: str, text: str) -> None:
    path = os.environ.get(env_name)
    if not path:
        return
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")


def emit_github(data: dict[str, Any], report_path: str) -> None:
    for violation in data["violations"]:
        print(f"::error title=ToolTax budget::{github_escape(violation['message'])}")
    report = data["report"]
    outputs = {
        "passed": str(data["passed"]).lower(),
        "violations": str(data["violations_count"]),
        "waste_percent": f"{float(report.get('estimated_waste_pct') or 0.0):.4f}",
        "schema_tokens": str(int(report.get("advertised_schema_tokens") or 0)),
        "duplicate_calls": str(int(report.get("duplicate_calls") or 0)),
        "errors": str(int(report.get("errors") or 0)),
        "observed_tokens": str(int(report.get("observed_tokens") or 0)),
        "report_path": report_path,
    }
    append_file_from_env("GITHUB_OUTPUT", "".join(f"{key}={value}\n" for key, value in outputs.items()))
    append_file_from_env("GITHUB_STEP_SUMMARY", render_markdown(data))


def budgets_from_args(args: argparse.Namespace) -> dict[str, int | float | None]:
    return {name: getattr(args, name) for name, *_ in BUDGET_SPECS}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tool-tax-budget", description="Fail CI when ToolTax metrics exceed configured budgets.")
    parser.add_argument("paths", nargs="*", help="ToolTax/Claude/Codex trace file(s) or directories.")
    parser.add_argument("--source", choices=["auto", "claude", "codex", "canonical"], default="auto")
    parser.add_argument("--max-waste-percent", type=float)
    parser.add_argument("--max-schema-tokens", type=int)
    parser.add_argument("--max-duplicate-calls", type=int)
    parser.add_argument("--max-errors", type=int)
    parser.add_argument("--max-observed-tokens", type=int)
    parser.add_argument("--max-median-latency-ms", type=float)
    parser.add_argument("--allow-missing-metrics", action="store_true")
    parser.add_argument("--allow-empty", action="store_true")
    parser.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    parser.add_argument("--output", "-o")
    parser.add_argument("--github-actions", action="store_true", help="Emit GitHub annotations, outputs, and job summary.")
    parser.add_argument("--from-action-env", action="store_true", help=argparse.SUPPRESS)
    return parser


def apply_action_env(args: argparse.Namespace) -> argparse.Namespace:
    args.paths = [os.environ.get("TOOLTAX_TRACE_PATH", "")]
    args.source = os.environ.get("TOOLTAX_SOURCE", "auto") or "auto"
    args.max_waste_percent = parse_optional_number(os.environ.get("TOOLTAX_MAX_WASTE_PERCENT"), float)
    args.max_schema_tokens = parse_optional_number(os.environ.get("TOOLTAX_MAX_SCHEMA_TOKENS"), int)
    args.max_duplicate_calls = parse_optional_number(os.environ.get("TOOLTAX_MAX_DUPLICATE_CALLS"), int)
    args.max_errors = parse_optional_number(os.environ.get("TOOLTAX_MAX_ERRORS"), int)
    args.max_observed_tokens = parse_optional_number(os.environ.get("TOOLTAX_MAX_OBSERVED_TOKENS"), int)
    args.max_median_latency_ms = parse_optional_number(os.environ.get("TOOLTAX_MAX_MEDIAN_LATENCY_MS"), float)
    args.allow_missing_metrics = parse_bool(os.environ.get("TOOLTAX_ALLOW_MISSING_METRICS"))
    args.allow_empty = parse_bool(os.environ.get("TOOLTAX_ALLOW_EMPTY"))
    args.format = "markdown"
    args.output = os.environ.get("TOOLTAX_REPORT_PATH", "tooltax-budget.md") or "tooltax-budget.md"
    args.github_actions = True
    return args


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.from_action_env:
        try:
            args = apply_action_env(args)
        except ValueError as exc:
            print(f"ToolTax configuration error: {exc}", file=sys.stderr)
            return EXIT_CONFIGURATION_ERROR

    args.paths = [path for path in args.paths if path]
    if not args.paths:
        print("ToolTax configuration error: at least one trace path is required.", file=sys.stderr)
        return EXIT_CONFIGURATION_ERROR

    report = analyze_paths(args.paths, args.source)
    data = evaluate_budget(
        report,
        budgets_from_args(args),
        allow_missing_metrics=args.allow_missing_metrics,
        allow_empty=args.allow_empty,
    )

    if args.format == "json":
        rendered = json.dumps(data, indent=2) + "\n"
    elif args.format == "markdown":
        rendered = render_markdown(data)
    else:
        rendered = render_text(data)

    if args.output:
        target = pathlib.Path(args.output)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)

    if args.github_actions:
        emit_github(data, args.output or "")

    return 0 if data["passed"] else EXIT_BUDGET_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
