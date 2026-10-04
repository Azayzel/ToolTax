# Plan

## v0.1 — local trace auditor
- [x] Portable `/tool-tax` Agent Skill
- [x] Canonical JSONL trace schema
- [x] Claude Code transcript adapter
- [x] Codex rollout adapter
- [x] Duplicate/error/empty/output-bloat signals
- [x] Per-server scoreboard + recommendations
- [x] Text, Markdown and JSON output
- [x] Dependency-free Python implementation
- [x] Tests and fixtures

## v0.2 — capture, not inference
- [x] Stdio MCP proxy/capture mode for exact proxy-observed server/tool timings
- [x] Capture `tools/list` definitions/schema size to estimate advertised schema overhead
- [x] De-duplicate schema catalogs across pagination and repeated tool-list refreshes
- [x] Protocol-transparency integration test with a real child process
- [ ] Session/project filtering and date windows
- [ ] Redaction pipeline before report sharing
- [ ] Baseline comparison: before vs after disabling/tuning a server
- [ ] Streamable HTTP capture strategy that preserves auth/session semantics

## v0.3 — team/CI
- [ ] GitHub Action with configurable waste budget
- [ ] Trend reports across commits/weeks
- [ ] SARIF/PR annotations for regressions
- [ ] Organization rollups without retaining raw prompts/results

## v1 direction
- [ ] Outcome-aware usefulness labels
- [ ] VS Code/Cursor companion UI
- [ ] OpenTelemetry ingestion/export
- [ ] Plugin adapters kept outside core package
