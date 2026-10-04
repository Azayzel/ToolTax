import importlib.util
import pathlib
import sys
import tempfile
import json
import unittest
from unittest import mock
import contextlib
import io

ROOT = pathlib.Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location("tool_tax", ROOT / "scripts" / "tool_tax.py")
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


class ToolTaxTests(unittest.TestCase):
    def test_explicit_customization_audit_finds_editor_prompts(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            workspace = home / "workspace"
            workspace.mkdir()
            prompt = home / "AppData/Roaming/Code/User/prompts/review.agent.md"
            prompt.parent.mkdir(parents=True)
            prompt.write_text("Review the implementation.", encoding="utf-8")
            args = mod.build_parser().parse_args(["--audit-customizations", "--share-safe"])
            with mock.patch.object(mod.pathlib.Path, "home", return_value=home), \
                    mock.patch.object(mod.pathlib.Path, "cwd", return_value=workspace), \
                    mock.patch.dict(mod.os.environ, {}, clear=True), \
                    mock.patch.object(mod, "discover_clients", side_effect=AssertionError("explicit traces must not discover clients")):
                data = mod.analyze_inputs([str(ROOT / "tests/fixtures/canonical.jsonl")], args)
            files = data["customizations"]["files"]
            self.assertEqual(len(files), 1)
            self.assertEqual((files[0]["kind"], files[0]["client"], files[0]["scope"]), ("agent", "copilot", "user"))

    def test_terminal_color_controls_and_compact_layout(self):
        class TerminalBuffer(io.StringIO):
            def isatty(self):
                return True
        for environment, animated in (({"CI": "true"}, False), ({"TERM": "dumb"}, False), ({"NO_COLOR": ""}, True)):
            output, errors = TerminalBuffer(), TerminalBuffer()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                    mock.patch.dict(mod.os.environ, environment, clear=True), \
                    mock.patch.object(mod.shutil, "get_terminal_size", return_value=mod.os.terminal_size((60, 24))):
                mod.main(["--demo"])
            self.assertNotIn("\033[", output.getvalue())
            self.assertEqual("\r" in errors.getvalue(), animated)
            if animated:
                self.assertIn("[#", output.getvalue())
        progress = mod.TerminalProgress()
        progress.stopped = mock.Mock()
        progress.stopped.wait.side_effect = [False, False, True]
        with mock.patch.object(progress, "draw") as draw:
            progress.animate()
        self.assertEqual(draw.call_args_list, [mock.call(1), mock.call(2)])

    def test_customization_frontmatter_and_repeated_paragraphs(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            paragraph = "Detailed guidance for tool usage and validation. " * 8
            (home / "all.instructions.md").write_text('---\napplyTo: "**"\n---\n' + paragraph + "\n\n" + paragraph, encoding="utf-8")
            yaml_parser = mock.Mock()
            yaml_parser.safe_load.return_value = {"applyTo": "**"}
            with mock.patch.object(mod.pathlib.Path, "home", return_value=home), \
                    mock.patch.object(mod.pathlib.Path, "cwd", return_value=home), \
                    mock.patch.dict(mod.os.environ, {}, clear=True), \
                    mock.patch.dict(sys.modules, {"yaml": yaml_parser}):
                audit = mod.audit_customizations([])
            item = audit["files"][0]
            self.assertEqual(item["applicability"], "declared-broad")
            self.assertIn("broad-scope", item["signals"])
            self.assertIn("repeated-paragraphs", item["signals"])
            with mock.patch.object(mod.pathlib.Path, "home", return_value=home), \
                    mock.patch.object(mod.pathlib.Path, "cwd", return_value=home), \
                    mock.patch.dict(mod.os.environ, {}, clear=True), \
                    mock.patch.dict(sys.modules, {"yaml": None}):
                audit = mod.audit_customizations([])
            self.assertEqual(audit["files"][0]["frontmatter_status"], "parser-unavailable")
            self.assertEqual(audit["files"][0]["applicability"], "unknown")

    def test_terminal_animation_is_interactive_only(self):
        class TerminalBuffer(io.StringIO):
            def isatty(self):
                return True
        for options, animated in ((["--demo"], True), (["--demo", "--plain"], False),
                                  (["--demo", "--format", "json"], False), (["--demo", "--format", "markdown"], False)):
            with self.subTest(options=options):
                output, errors = TerminalBuffer(), TerminalBuffer()
                with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors), \
                        mock.patch.dict(mod.os.environ, {}, clear=True):
                    self.assertEqual(mod.main(options), 0)
                self.assertEqual("\033[" in errors.getvalue(), animated)
                self.assertEqual("T O O L T A X" in output.getvalue(), animated)
                if "json" in options:
                    self.assertEqual(json.loads(output.getvalue())["calls"], 10)
        output, errors = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
            mod.main(["--demo"])
        self.assertNotIn("\033[", output.getvalue() + errors.getvalue())

    def test_terminal_progress_cleans_up_on_failure(self):
        errors = io.StringIO()
        with contextlib.redirect_stderr(errors):
            progress = mod.TerminalProgress(enabled=True)
            with self.assertRaises(ValueError), progress:
                progress.update("Test phase")
                progress.draw(3)
                raise ValueError("failure")
        self.assertFalse(progress.worker.is_alive())
        self.assertTrue(errors.getvalue().endswith("\r\033[2K"))
        self.assertIn("Test phase", errors.getvalue())

    def test_customization_audit_keeps_static_and_observed_evidence_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            skill = home / ".agents" / "skills" / "example" / "SKILL.md"
            skill.parent.mkdir(parents=True)
            text = "Always use read_file.\n\n" + "A detailed instruction paragraph. " * 12
            skill.write_text(text, encoding="utf-8")
            (home / "AGENTS.md").write_text(text, encoding="utf-8")
            (home / "CLAUDE.md").write_text("Never use read_file.\n", encoding="utf-8")
            calls = [mod.ToolCall("copilot", "session", "read", "read_file", "builtin",
                                 arguments={"filePath": str(skill)}, result=text, model="observed-model")]
            with mock.patch.object(mod.pathlib.Path, "home", return_value=home), \
                    mock.patch.object(mod.pathlib.Path, "cwd", return_value=home), \
                    mock.patch.dict(mod.os.environ, {}, clear=True):
                audit = mod.audit_customizations(calls, share_safe=True)
            self.assertEqual(len(audit["files"]), 3)
            self.assertEqual(len(audit["duplicate_groups"]), 1)
            self.assertEqual(len(audit["possible_conflicts"]), 1)
            read_files = [item for item in audit["files"] if item["observed_reads"]]
            self.assertEqual(len(read_files), 1)
            self.assertEqual(read_files[0]["evidence"], "installed")
            self.assertEqual(read_files[0]["observed_reads"][0]["evidence"], "observed-file-read")
            self.assertTrue(read_files[0]["tool_mentions"])
            self.assertNotIn(str(home), json.dumps(audit))
            self.assertNotIn(text, json.dumps(audit))
            data = self.discover_in(home, ("--share-safe",))
            self.assertEqual(data["observed_tokens"], 0)
            self.assertGreater(data["customizations"]["static_tokens"], 0)
            self.assertNotIn("customizations", self.discover_in(home, ("--no-customizations",)))

    def test_customization_audit_limits_and_excludes_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            (home / "node_modules").mkdir()
            (home / "node_modules" / "SKILL.md").write_text("ignored", encoding="utf-8")
            (home / "AGENTS.md").write_bytes(b"x" * (4 * 1024 * 1024 + 1))
            with mock.patch.object(mod.pathlib.Path, "home", return_value=home), \
                    mock.patch.object(mod.pathlib.Path, "cwd", return_value=home), \
                    mock.patch.dict(mod.os.environ, {}, clear=True):
                audit = mod.audit_customizations([])
            self.assertEqual(len(audit["files"]), 1)
            self.assertEqual(audit["files"][0]["status"], "size-limit")
            self.assertEqual(audit["static_tokens"], 0)

    def test_builtin_recommendations_are_tool_specific(self):
        calls = [mod.ToolCall("copilot", "session", str(index), "run_in_terminal", "builtin",
                             arguments={"command": str(index)}, result="text" * 3000) for index in range(2)]
        calls.append(mod.ToolCall("copilot", "session", "image", "view_image", "builtin", result="",
                                 media_parts=[{"mime_type": "image/png", "encoded_chars": 10000, "token_cost": None}]))
        data = mod.report_data(calls, mod.analyze(calls), mod.ParseStats(), [])
        self.assertEqual(data["servers"][0]["recommendation"], "review individual tools")
        tools = {row["tool"]: row for row in data["builtin_tools"]}
        self.assertEqual(tools["run_in_terminal"]["recommendation"], "review large text outputs")
        self.assertEqual(tools["view_image"]["waste_tokens"], 0)
        self.assertEqual(tools["view_image"]["recommendation"], "text signals clear; media unpriced")
        self.assertEqual(data["media_attachments"], 1)
        self.assertIsNone(data["media_token_cost"])
        for printer in (mod.print_text, mod.print_markdown):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                printer(data)
            self.assertIn("builtin/run_in_terminal", output.getvalue())
            self.assertIn("builtin/view_image", output.getvalue())
            self.assertIn("token cost unknown", output.getvalue())

    def test_copilot_media_is_not_text_or_empty_output(self):
        records = [{"sessionId": "session", "requests": [{"result": {"metadata": {
            "toolCallRounds": [{"toolCalls": [
                {"id": "mixed", "name": "run_in_terminal", "arguments": "{}"},
                {"id": "image", "name": "view_image", "arguments": "{}"},
            ]}],
            "toolCallResults": {
                "mixed": {"content": [{"value": "done"}, {"mimeType": "image/png", "data": "A" * 40000}]},
                "image": {"content": [{"mimeType": "image/png", "data": "A" * 40000}]},
            },
        }}}]}]
        calls = mod.parse_copilot(records, "session", "trace", mod.ParseStats())
        self.assertEqual([call.output_tokens for call in calls], [1, 0])
        self.assertFalse(calls[1].empty_result)
        self.assertEqual(calls[0].media_parts[0]["encoded_chars"], 40000)
        self.assertFalse(any(finding.kind in {"empty-result", "output-bloat"} for finding in mod.analyze(calls)))

    def test_copilot_saved_session_pairs_calls_and_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            session = {"version": 3, "sessionId": "session", "requests": [{
                "requestId": "request", "modelId": "copilot/observed-model",
                "response": [{"kind": "toolInvocationSerialized", "toolCallId": "call",
                              "toolId": "mcp_docs_lookup", "isComplete": True,
                              "source": {"type": "mcp", "label": "Docs", "serverLabel": "docs"}}],
                "result": {"metadata": {
                    "toolCallRounds": [{"toolCalls": [{"id": "call__vscode-12345", "name": "mcp_docs_lookup", "arguments": "{\"query\":\"test\"}"}]}],
                    "toolCallResults": {"call__vscode-12345": {"content": [{"value": "found"}]}},
                }},
            }]}
            path = self.write_local(home, "AppData/Roaming/Code/User/workspaceStorage/work/chatSessions/session.json", session)
            data = self.discover_in(home)
            self.assertEqual(data["calls"], 1)
            self.assertEqual(data["sources"], {"copilot": 1})
            self.assertEqual(data["correlations"][0]["model"], "copilot/observed-model")
            calls = mod.load_file(path, "auto", mod.ParseStats())
            self.assertEqual(calls[0].arguments, {"query": "test"})
            self.assertEqual(calls[0].result, "found")
            self.assertEqual((calls[0].server, calls[0].tool), ("docs", "mcp_docs_lookup"))
            self.assertEqual(calls[0].latency_ms, None)

    def test_empty_mcp_config_is_not_a_parse_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "mcp.json"
            path.write_text("", encoding="utf-8")
            self.assertEqual(mod.read_client_config(path), {})

    def test_copilot_journal_replays_updates_without_duplicate_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            request = {"requestId": "request", "modelId": "recorded-model", "result": {"metadata": {
                "toolCallRounds": [{"toolCalls": [{"id": "call", "name": "mcp__docs__lookup", "arguments": "{}"}]}],
                "toolCallResults": {},
            }}}
            entries = [
                {"kind": 0, "v": {"version": 3, "sessionId": "session", "requests": []}},
                {"kind": 2, "k": ["requests"], "v": [request, {"requestId": "removed"}]},
                {"kind": 2, "k": ["requests"], "i": 1},
                {"kind": 1, "k": ["requests", 0, "result", "metadata", "toolCallResults"], "v": {"call": {"content": [{"value": "old"}]}}},
                {"kind": 1, "k": ["requests", 0, "result", "metadata", "toolCallResults", "call"], "v": {"content": [{"value": "final"}], "isError": True}},
                {"kind": 1, "k": ["customTitle"], "v": "temporary"},
                {"kind": 3, "k": ["customTitle"]},
            ]
            path = home / "session.jsonl"
            path.write_text("\n".join(json.dumps(entry) for entry in entries), encoding="utf-8")
            stats = mod.ParseStats()
            calls = mod.load_file(path, "auto", stats)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].result, "final")
            self.assertTrue(calls[0].error)
            self.assertEqual(calls[0].model, "recorded-model")
            self.assertEqual(stats.source_counts, {"copilot": 1})
            self.assertEqual(stats.lines, len(entries))

    def test_copilot_journal_rejects_invalid_operations(self):
        for operation in ({"kind": 7}, {"kind": 1, "k": ["missing", "field"], "v": 1}, {"kind": 2, "k": ["requests"], "i": -1}):
            with self.subTest(operation=operation), self.assertRaises(ValueError):
                mod.replay_copilot_journal([{"kind": 0, "v": {"requests": []}}, operation])

    def test_copilot_malformed_journal_is_not_partially_scored(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            path = self.write_local(home, "AppData/Roaming/Code/User/workspaceStorage/work/chatSessions/session.jsonl",
                                    {"kind": 0, "v": {"sessionId": "session", "requests": []}})
            with path.open("a", encoding="utf-8") as handle:
                handle.write('\n{"kind":1,')
            data = self.discover_in(home, ("--share-safe",))
            self.assertEqual(data["files_scanned"], 0)
            self.assertEqual(data["calls"], 0)
            self.assertEqual(data["discovery"]["clients"][0]["history"][0]["status"], "unreadable-or-invalid")
            self.assertEqual(len(data["discovery"]["warnings"]), 1)

    def test_copilot_discovery_uses_newest_session_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            session = {"sessionId": "same-session", "requests": [{"requestId": "request", "result": {"metadata": {
                "toolCallRounds": [{"toolCalls": [{"id": "call", "name": "read_file", "arguments": "{}"}]}],
                "toolCallResults": {"call": {"content": [{"value": "old"}]}},
            }}}]}
            older = self.write_local(home, "AppData/Roaming/Code/User/workspaceStorage/one/chatSessions/session.json", session)
            session["requests"][0]["result"]["metadata"]["toolCallResults"]["call"]["content"][0]["value"] = "new"
            newer = self.write_local(home, "AppData/Roaming/Code/User/workspaceStorage/two/chatSessions/session.json", session)
            mod.os.utime(older, (100, 100))
            mod.os.utime(newer, (200, 200))
            data = self.discover_in(home)
            self.assertEqual(data["calls"], 1)
            self.assertEqual(data["discovery"]["clients"][0]["observed_calls"], 1)
            self.assertTrue(any(item["status"] == "superseded-session-copy" for item in data["discovery"]["clients"][0]["history"]))
            newer.write_text(json.dumps({"sessionId": "same-session", "requests": []}), encoding="utf-8")
            self.assertEqual(self.discover_in(home)["calls"], 0)

    def test_duplicate_calls_do_not_cross_sessions(self):
        calls = [mod.ToolCall("copilot", session, "call", "read", "builtin", arguments={"path": "same"}, result="found")
                 for session in ("first", "second")]
        self.assertFalse(any(finding.kind == "duplicate-call" for finding in mod.analyze(calls)))

    def discover_in(self, home, options=(), environ=None):
        with mock.patch.object(mod.pathlib.Path, "home", return_value=home), \
                mock.patch.object(mod.pathlib.Path, "cwd", return_value=home), \
                mock.patch.dict(mod.os.environ, environ or {}, clear=True):
            return mod.analyze_inputs([], mod.build_parser().parse_args(list(options)))

    def write_local(self, home, relative, value):
        path = home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def test_default_discovery_reports_client_without_transcripts(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            claude = home / ".claude"
            (claude / "sessions").mkdir(parents=True)
            (claude / "settings.json").write_text(json.dumps({
                "model": "configured-model",
                "mcpServers": {"docs": {"command": "server", "env": {"TOKEN": "secret-value"}}},
            }), encoding="utf-8")
            (claude / "sessions" / "state.json").write_text('{"sessionId":"active"}', encoding="utf-8")
            with mock.patch.object(mod.pathlib.Path, "home", return_value=home), \
                    mock.patch.object(mod.pathlib.Path, "cwd", return_value=home), \
                    mock.patch.dict(mod.os.environ, {}, clear=True):
                data = mod.analyze_inputs([], mod.build_parser().parse_args([]))
            client = next(item for item in data["discovery"]["clients"] if item["id"] == "claude")
            self.assertTrue(any(item["kind"] == "sessions" for item in client["artifacts"]))
            self.assertEqual(client["mcp_servers"][0]["transport"], "stdio")
            self.assertEqual(data["calls"], 0)
            self.assertEqual(data["files_scanned"], 0)
            self.assertNotIn("secret-value", json.dumps(data))

    def test_discovery_registry_and_client_storage(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            for folder in (".claude", ".codex", ".cursor", ".continue", ".copilot", ".gemini", ".config/opencode"):
                (home / folder).mkdir(parents=True)
            self.write_local(home, "AppData/Roaming/Code/User/workspaceStorage/work/chatSessions/state.json", {"requests": []})
            self.write_local(home, "AppData/Roaming/Cursor/User/globalStorage/state.vscdb", {})
            data = self.discover_in(home)
            clients = {client["id"]: client for client in data["discovery"]["clients"]}
            self.assertTrue({"claude", "codex", "cursor", "continue", "copilot", "gemini", "opencode"} <= clients.keys())
            self.assertTrue(any(item["kind"] == "workspace storage" for item in clients["copilot"]["artifacts"]))
            self.assertEqual(clients["cursor"]["history"][0]["status"], "opaque-storage")
            self.assertEqual(data["calls"], 0)

    def test_discovery_jsonc_transports_and_privacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            path = self.write_local(home, ".vscode/mcp.json", {})
            path.write_text('''{
                // Keep URL punctuation intact.
                "servers": {
                    "docs": {"url": "https://private.example/mcp?token=secret", "headers": {"Authorization": "secret"}},
                    "events": {"type": "sse", "url": "https://private.example/events", "disabled": true},
                    "local": {"command": "private-command", "args": ["secret-argument"], "env": {"KEY": "secret"}},
                },
            }''', encoding="utf-8")
            parsed = mod.read_client_config(path)
            self.assertEqual(parsed["servers"]["docs"]["url"], "https://private.example/mcp?token=secret")
            data = self.discover_in(home, ("--share-safe",))
            servers = data["discovery"]["clients"][0]["mcp_servers"]
            self.assertEqual({server["transport"] for server in servers}, {"stdio", "http", "sse"})
            self.assertTrue(next(server for server in servers if server["server"] == "events")["disabled"])
            text = json.dumps(data)
            for forbidden in ("private.example", "secret", "private-command", "secret-argument", home.name):
                self.assertNotIn(forbidden, text)
            for row in data["correlations"]:
                self.assertEqual((row["model"], row["calls"], row["tool"]), ("unknown", 0, None))

    def test_discovery_observed_model_and_json_session_wrapper(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            records = [
                {"type": "assistant", "message": {"model": "observed-model", "content": [
                    {"type": "tool_use", "id": "call", "name": "mcp__docs__lookup", "input": {"q": "test"}},
                ]}},
                {"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": "call", "content": "found"},
                ]}},
            ]
            self.write_local(home, ".claude/sessions/export.json", {"messages": records})
            self.write_local(home, ".claude/settings.json", {"model": "configured-model"})
            data = self.discover_in(home)
            self.assertEqual(data["files_scanned"], 1)
            self.assertEqual(data["calls"], 1)
            row = data["correlations"][0]
            self.assertEqual((row["model"], row["client"], row["server"], row["tool"]),
                             ("observed-model", "claude", "docs", "lookup"))
            self.assertEqual(row["evidence"], "observed")
            self.assertEqual(data["sources"], {"claude": 1})

    def test_discovery_source_filter_and_environment_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            self.write_local(home, "custom-claude/settings.json", {"model": "custom"})
            (home / ".cursor").mkdir()
            data = self.discover_in(home, ("--source", "claude"), {"CLAUDE_CONFIG_DIR": str(home / "custom-claude")})
            self.assertEqual([client["id"] for client in data["discovery"]["clients"]], ["claude"])
            self.assertEqual(data["discovery"]["clients"][0]["models"][0]["model"], "custom")
            canonical = self.discover_in(home, ("--source", "canonical"))
            self.assertEqual(canonical["discovery"]["clients"], [])

    def test_discovery_does_not_score_installed_example_traces(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            record = {"event": "tool_call", "tool": "example", "arguments": {}}
            for relative in (".claude/plugins/sample/sessions/example.jsonl", ".continue/skills/tooltax/sessions/example.jsonl", ".cursor/examples/example.jsonl"):
                self.write_local(home, relative, record)
            self.write_local(home, ".claude/plugins/sample/.mcp.json", {"mcpServers": {"plugin-docs": {"command": "server"}}})
            self.write_local(home, ".continue/mcpServers/docs.json", {"mcpServers": [{"name": "docs", "command": "server"}]})
            data = self.discover_in(home)
            self.assertEqual((data["calls"], data["files_scanned"]), (0, 0))
            self.assertEqual({row["server"] for row in data["correlations"]}, {"plugin-docs", "docs"})

    def test_discovery_toml_and_codex_turn_models(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            path = self.write_local(home, ".codex/config.toml", {})
            path.write_text('model = "configured"\n[mcp_servers.docs]\ncommand = "server"\n', encoding="utf-8")
            records = [
                {"type": "turn_context", "payload": {"model": "first-model"}},
                {"type": "response_item", "payload": {"type": "function_call", "call_id": "one", "name": "mcp__docs__lookup", "arguments": "{}"}},
                {"type": "turn_context", "payload": {"model": "second-model"}},
                {"type": "response_item", "payload": {"type": "function_call", "call_id": "two", "name": "mcp__docs__lookup", "arguments": "{}"}},
            ]
            trace = self.write_local(home, ".codex/archived_sessions/session.jsonl", {})
            trace.write_text("\n".join(json.dumps(record) for record in records), encoding="utf-8")
            data = self.discover_in(home)
            self.assertEqual({row["model"] for row in data["correlations"] if row["evidence"] == "observed"}, {"first-model", "second-model"})
            if sys.version_info >= (3, 11) or importlib.util.find_spec("tomli"):
                self.assertEqual(data["discovery"]["clients"][0]["mcp_servers"][0]["server"], "docs")
            else:
                self.assertIn("tomli", data["discovery"]["warnings"][0]["message"])

    def test_discovery_invalid_config_and_unsupported_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            path = self.write_local(home, ".claude/settings.json", {})
            path.write_text("invalid private-content", encoding="utf-8")
            self.write_local(home, ".claude/sessions/state.json", {"sessionId": "active"})
            self.write_local(home, ".continue/sessions/chat.json", {"history": [{"role": "user", "content": "private-prompt"}]})
            data = self.discover_in(home)
            self.assertEqual(len(data["discovery"]["warnings"]), 1)
            self.assertNotIn("private-content", json.dumps(data))
            self.assertNotIn("private-prompt", json.dumps(data))
            self.assertEqual((data["files_scanned"], data["calls"]), (0, 0))

    def test_discovery_missing_yaml_dependency_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            self.write_local(home, ".continue/config.yaml", {})
            with mock.patch.dict(sys.modules, {"yaml": None}):
                data = self.discover_in(home)
            self.assertIn("PyYAML", data["discovery"]["warnings"][0]["message"])

    def test_discovery_yaml_config_when_available(self):
        if importlib.util.find_spec("yaml") is None:
            self.skipTest("Optional PyYAML is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            path = self.write_local(home, ".continue/config.yaml", {})
            path.write_text('models:\n  - model: local-model\nmcpServers:\n  - name: docs\n    command: server\n', encoding="utf-8")
            data = self.discover_in(home)
            client = data["discovery"]["clients"][0]
            self.assertEqual(client["models"][0]["model"], "local-model")
            self.assertEqual(client["mcp_servers"][0]["transport"], "stdio")

    def test_explicit_trace_does_not_discover_clients(self):
        with mock.patch.object(mod, "discover_clients", side_effect=AssertionError("unexpected discovery")):
            data = mod.analyze_inputs([str(ROOT / "tests/fixtures/canonical.jsonl")], mod.build_parser().parse_args([]))
        self.assertEqual(data["calls"], 3)
        self.assertNotIn("discovery", data)

    def test_discovery_text_and_markdown_include_inventory(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            self.write_local(home, ".claude/settings.json", {"mcpServers": {"docs": {"command": "server"}}})
            data = self.discover_in(home, ("--share-safe",))
            for printer in (mod.print_text, mod.print_markdown):
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    printer(data)
                self.assertIn("ToolTax local discovery", output.getvalue())
                self.assertIn("Claude Code / Claude VS Code", output.getvalue())
                self.assertIn("MODEL -> CLIENT -> MCP SERVER -> TOOL", output.getvalue())
                self.assertNotIn(home.name, output.getvalue())

    def test_discovery_configured_model_has_no_invented_server_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = pathlib.Path(tmp)
            self.write_local(home, ".continue/config.json", {"models": [{"model": "local-model"}]})
            data = self.discover_in(home)
            row = data["correlations"][0]
            self.assertEqual((row["model"], row["client"], row["server"], row["calls"]), ("local-model", "continue", None, 0))
            self.assertEqual(row["evidence"], "configured-model")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                mod.print_text(data)
            self.assertIn("MCP server unknown", output.getvalue())

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
