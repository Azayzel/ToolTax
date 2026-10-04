#!/usr/bin/env python3
"""Redact an existing ToolTax/JSONL trace for safer sharing."""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

from tool_tax_redaction import Redactor


def redact_file(source: pathlib.Path, target: pathlib.Path) -> tuple[int, int]:
    redactor = Redactor()
    read = written = 0
    target.parent.mkdir(parents=True, exist_ok=True)
    with source.open("r", encoding="utf-8", errors="replace") as src, target.open("w", encoding="utf-8") as dst:
        for line in src:
            read += 1
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                # Do not copy malformed/raw text into a supposedly share-safe trace.
                continue
            dst.write(json.dumps(redactor.redact(value), ensure_ascii=False, separators=(",", ":")) + "\n")
            written += 1
    return read, written


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Create a share-safer redacted copy of a ToolTax/JSONL trace.")
    parser.add_argument("source", help="Input JSONL trace.")
    parser.add_argument("output", help="Redacted output JSONL path. Refuses to overwrite the input.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    source = pathlib.Path(args.source).expanduser().resolve()
    target = pathlib.Path(args.output).expanduser().resolve()
    if source == target:
        raise SystemExit("refusing to overwrite the source trace; choose a different output path")
    if not source.is_file():
        raise SystemExit(f"source trace not found: {source}")
    read, written = redact_file(source, target)
    print(f"redacted {written}/{read} JSONL records -> {target}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
