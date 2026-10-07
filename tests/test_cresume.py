"""Synthetic rollout and CLI tests; never launch a real Codex session."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import importlib.machinery
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SCRIPT = Path(__file__).resolve().parents[1] / "cresume"
loader = importlib.machinery.SourceFileLoader("cresume", str(SCRIPT))
spec = importlib.util.spec_from_loader(loader.name, loader)
cresume = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cresume
loader.exec_module(cresume)

ROOT_ID = "01a1132b-6e92-7cd3-80a3-dd2bac27686d"
OTHER_ID = "01a11303-6602-7770-9a11-6697e91f193b"
CHILD_ID = "01a1132b-6eff-7941-b1b5-ecd3d673b7ff"


def metadata(**fields):
    payload = {"id": ROOT_ID, "cwd": "/code/bonsai", "source": "cli"}
    payload.update(fields)
    return {"type": "session_meta", "payload": payload}


def event(message):
    return {"type": "event_msg", "payload": {"type": "user_message", "message": message}}


def response(role, content):
    if isinstance(content, str):
        content = [{"type": "input_text", "text": content}]
    return {
        "type": "response_item",
        "payload": {"type": "message", "role": role, "content": content},
    }


class RolloutFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.codex_home = Path(self.temporary.name) / ".codex"
        self.directory = self.codex_home / "sessions"
        self.directory.mkdir(parents=True)

    def write(self, name, records, mtime=1000):
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "\n".join(json.dumps(record) if not isinstance(record, str) else record
                      for record in records) + "\n",
            encoding="utf-8",
        )
        os.utime(path, (mtime, mtime))
        return path

    def root(self):
        path = self.write("rollout-root.jsonl", [metadata(), event("Implement sparse attention")])
        return cresume.parse_rollout(path)


class ParsingTests(RolloutFixture):
    def test_normal_root_session(self):
        path = self.write("2026/10/07/rollout-root.jsonl", [
            metadata(session_id=ROOT_ID, parent_thread_id=None, thread_source="user",
                     timestamp="2026-10-07T10:22:00Z", git={"branch": "main"}),
            event("Implement sparse attention"),
        ])
        sessions = cresume.load_sessions(self.directory)
        self.assertEqual(len(sessions), 1)
        session = sessions[0]
        self.assertEqual(session.session_id, ROOT_ID)
        self.assertEqual(session.project, "bonsai")
        self.assertEqual(session.cwd, "/code/bonsai")
        self.assertEqual(session.branch, "main")
        self.assertEqual(session.timestamp, "2026-10-07T10:22:00Z")
        self.assertEqual(session.rollout, path)

    def test_guardian_child_is_excluded_before_deduplication(self):
        root = self.root()
        self.write("rollout-child.jsonl", [
            metadata(id=CHILD_ID, session_id=ROOT_ID, parent_thread_id=ROOT_ID),
            event("Review this action"),
        ], mtime=2000)
        self.assertEqual(cresume.load_sessions(self.directory), [root])

    def test_subagent_source_without_parent_is_excluded(self):
        path = self.write("rollout-subagent.jsonl", [
            metadata(source={"subagent": {"other": "guardian"}}), event("Review"),
        ])
        self.assertIsNone(cresume.parse_rollout(path))

    def test_guardian_thread_source_is_excluded(self):
        path = self.write("rollout-guardian.jsonl", [metadata(thread_source="guardian_review")])
        self.assertIsNone(cresume.parse_rollout(path))

    def test_nested_metadata_keeps_outer_git_and_source(self):
        path = self.write("rollout-nested.jsonl", [{
            "type": "session_meta",
            "payload": {
                "meta": {"id": ROOT_ID, "cwd": "/code/nested"},
                "git": {"branch": "feature-x"}, "source": "vscode",
                "parent_thread_id": None, "thread_source": "user",
            },
        }, event("Fix the regression")])
        session = cresume.parse_rollout(path)
        self.assertEqual(session.project, "nested")
        self.assertEqual(session.branch, "feature-x")

    def test_conflicting_nested_metadata_cannot_hide_internal_session(self):
        for key, value in (
            ("parent_thread_id", ROOT_ID),
            ("source", {"subagent": {"other": "guardian"}}),
            ("thread_source", "guardian_review"),
        ):
            for internal_layer in ("outer", "nested"):
                with self.subTest(key=key, layer=internal_layer):
                    payload = {"id": ROOT_ID, "meta": {"id": ROOT_ID}}
                    payload[key] = value if internal_layer == "outer" else None
                    payload["meta"][key] = value if internal_layer == "nested" else None
                    path = self.write("rollout-conflict.jsonl", [
                        {"type": "session_meta", "payload": payload}, event("Review"),
                    ])
                    self.assertIsNone(cresume.parse_rollout(path))

    def test_duplicate_logical_uuid_keeps_newer_rollout(self):
        self.write("rollout-old.jsonl", [metadata(), event("Old")], mtime=1000)
        newer = self.write("rollout-new.jsonl", [
            metadata(id=OTHER_ID, session_id=ROOT_ID), event("New"),
        ], mtime=2000)
        sessions = cresume.load_sessions(self.directory)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0].rollout, newer)
        self.assertEqual(sessions[0].preview, "New")

    def test_broken_json_and_unexpected_record_shapes_are_skipped(self):
        path = self.write("rollout-broken-lines.jsonl", [
            "{broken", None, [], {"type": "session_meta", "payload": []},
            metadata(), "not json", {"type": "event_msg", "payload": None},
            event("Still works"), "{truncated",
        ])
        self.assertEqual(cresume.parse_rollout(path).preview, "Still works")

    def test_entirely_malformed_rollout_is_skipped(self):
        path = self.write(f"rollout-{ROOT_ID}.jsonl", ["{broken", "not json"])
        self.assertIsNone(cresume.parse_rollout(path))

    def test_missing_fields_use_filename_and_placeholders(self):
        path = self.write(f"rollout-{ROOT_ID}.jsonl", [
            {"type": "session_meta", "payload": {}},
        ])
        session = cresume.parse_rollout(path)
        self.assertEqual(session.session_id, ROOT_ID)
        self.assertIsNone(session.cwd)
        self.assertEqual(session.project, "(unknown)")
        self.assertIn("(no user message)", cresume.format_session(session))

    def test_missing_metadata_can_use_filename_with_actual_user_message(self):
        path = self.write(f"rollout-{ROOT_ID}.jsonl", [event("Hello")])
        self.assertEqual(cresume.parse_rollout(path).session_id, ROOT_ID)

    def test_metadata_uuid_is_preferred_over_filename(self):
        path = self.write(f"rollout-{OTHER_ID}.jsonl", [metadata(id=ROOT_ID.upper())])
        self.assertEqual(cresume.parse_rollout(path).session_id, ROOT_ID)

    def test_invalid_uuid_is_not_presented(self):
        path = self.write("rollout-invalid.jsonl", [metadata(id="--some-option")])
        self.assertIsNone(cresume.parse_rollout(path))

    def test_invalid_field_types_do_not_crash(self):
        path = self.write(f"rollout-{ROOT_ID}.jsonl", [
            metadata(id=[], cwd=42, git=[], timestamp={}), event({"unexpected": "shape"}),
        ])
        session = cresume.parse_rollout(path)
        self.assertIsNone(session.cwd)
        self.assertEqual(session.branch, "")
        self.assertEqual(session.preview, "")

    def test_unreadable_or_removed_rollout_is_skipped(self):
        self.assertIsNone(cresume.parse_rollout(self.directory / "rollout-absent.jsonl"))
        path = self.write("rollout-unreadable.jsonl", [metadata()])
        with mock.patch.object(Path, "open", side_effect=PermissionError("unreadable")):
            self.assertIsNone(cresume.parse_rollout(path))

    def test_cwd_filter_normalizes_components_and_symlinks(self):
        project = Path(self.temporary.name) / "project"
        project.mkdir()
        alias = project.parent / "alias"
        alias.symlink_to(project, target_is_directory=True)
        self.write("rollout-root.jsonl", [metadata(cwd=str(alias / ".." / "alias"))])
        self.write("rollout-child-dir.jsonl", [metadata(id=OTHER_ID, cwd=str(project / "child"))])
        self.write("rollout-unknown-cwd.jsonl", [metadata(id=CHILD_ID, cwd=None)])
        sessions = cresume.load_sessions(self.directory, cwd=str(project) + "/")
        self.assertEqual([session.session_id for session in sessions], [ROOT_ID])

    def test_cwd_filter_applies_to_the_newest_duplicate(self):
        self.write("rollout-old.jsonl", [metadata(cwd="/code/old")], mtime=1000)
        self.write("rollout-new.jsonl", [metadata(cwd="/code/new")], mtime=2000)
        self.assertEqual(cresume.load_sessions(self.directory, cwd="/code/old"), [])

    def test_first_actual_user_event_is_the_preview(self):
        path = self.write("rollout-first-message.jsonl", [
            metadata(), response("system", "System prompt"),
            response("developer", "Instructions"), response("assistant", "Assistant output"),
            response("tool", "Tool output"),
            response("user", "# AGENTS.md instructions for /code\n<INSTRUCTIONS>hidden</INSTRUCTIONS>"),
            response("user", "<environment_context>cwd: /code</environment_context>"),
            event("<user_instructions>hidden</user_instructions>"),
            event("  Fix\n the\t regression  "), event("A later user message"),
        ])
        self.assertEqual(cresume.parse_rollout(path).preview, "Fix the regression")

    def test_user_response_item_text_blocks_are_supported(self):
        path = self.write("rollout-response.jsonl", [metadata(), response("user", [
            {"type": "input_text", "text": "# AGENTS.md instructions for /code\nhidden"},
            {"type": "input_text", "text": "<environment_context>hidden</environment_context>"},
            {"type": "input_image", "image_url": "ignored"},
            {"type": "input_text", "text": "Help me"},
            {"type": "text", "text": "understand this paper"},
        ])])
        self.assertEqual(cresume.parse_rollout(path).preview, "Help me understand this paper")

    def test_context_can_share_a_text_block_with_real_user_text(self):
        path = self.write("rollout-mixed.jsonl", [metadata(), event(
            "<environment_context>hidden</environment_context>\nBuild the tool"
        )])
        self.assertEqual(cresume.parse_rollout(path).preview, "Build the tool")

    def test_preview_is_truncated_and_terminal_controls_are_removed(self):
        path = self.write("rollout-long.jsonl", [metadata(), event("A\x1b\x00\u202e" + "b" * 150)])
        preview = cresume.parse_rollout(path).preview
        self.assertEqual(len(preview), 100)
        self.assertTrue(preview.endswith("…"))
        self.assertTrue(all(char.isprintable() for char in preview))

    def test_sort_uses_mtime_instead_of_session_creation_timestamp(self):
        self.write("rollout-older.jsonl", [metadata(timestamp="2099-01-01")], mtime=1000)
        self.write("rollout-newer.jsonl", [metadata(id=OTHER_ID, timestamp="2000-01-01")], mtime=2000)
        self.assertEqual(
            [session.session_id for session in cresume.load_sessions(self.directory)],
            [OTHER_ID, ROOT_ID],
        )


class PickerTests(RolloutFixture):
    def setUp(self):
        super().setUp()
        self.sessions = [self.root()]

    def test_fzf_retains_uuid_and_passes_query_as_an_argument(self):
        row = f"{ROOT_ID}\t{cresume.format_session(self.sessions[0])}\n"
        result = subprocess.CompletedProcess([], 0, stdout=row)
        with mock.patch.object(cresume.subprocess, "run", return_value=result) as run:
            selected = cresume.choose_with_fzf(self.sessions, "bonsai main")
        self.assertEqual(selected, self.sessions[0])
        args = run.call_args.args[0]
        self.assertIn("--with-nth=2..", args)
        self.assertEqual(args[args.index("--query") + 1], "bonsai main")
        self.assertEqual(run.call_args.kwargs["input"], row)
        self.assertNotIn("FZF_DEFAULT_OPTS", run.call_args.kwargs["env"])

    def test_fzf_cancellation_and_no_matches_are_clean(self):
        for code in (1, 130):
            with self.subTest(code=code), mock.patch.object(
                cresume.subprocess, "run",
                return_value=subprocess.CompletedProcess([], code, stdout=""),
            ):
                self.assertIsNone(cresume.choose_with_fzf(self.sessions))

    def test_fzf_failures_and_unknown_selections_are_reported(self):
        for code, output in ((2, ""), (0, "not a session")):
            with self.subTest(code=code), mock.patch.object(
                cresume.subprocess, "run",
                return_value=subprocess.CompletedProcess([], code, stdout=output),
            ), self.assertRaises(RuntimeError):
                cresume.choose_with_fzf(self.sessions)

    def test_basic_menu_retries_invalid_input(self):
        with mock.patch("builtins.input", side_effect=["invalid", "99", "1"]), redirect_stdout(io.StringIO()):
            self.assertEqual(cresume.choose_basic(self.sessions), self.sessions[0])

    def test_basic_menu_cancels_cleanly(self):
        for answer in ("", "q", "\x1b", EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=answer), mock.patch("builtins.input", side_effect=[answer]), redirect_stdout(io.StringIO()):
                self.assertIsNone(cresume.choose_basic(self.sessions))

    def test_basic_menu_limits_to_40(self):
        sessions = [replace(self.sessions[0], preview=f"Session {i}") for i in range(45)]
        output = io.StringIO()
        with mock.patch("builtins.input", return_value=""), redirect_stdout(output):
            cresume.choose_basic(sessions)
        self.assertIn("Session 39", output.getvalue())
        self.assertNotIn("Session 40", output.getvalue())
        self.assertIn("newest 40 of 45", output.getvalue())

    def test_basic_query_filters_before_limit(self):
        target = replace(self.sessions[0], branch="FEATURE-X", preview="Fix regression")
        sessions = [replace(target, preview="Unrelated") for _ in range(45)] + [target]
        with mock.patch("builtins.input", return_value="1"), redirect_stdout(io.StringIO()):
            self.assertEqual(cresume.choose_basic(sessions, "bonsai feature-x regression"), target)


class CliTests(RolloutFixture):
    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.stdout = io.StringIO()
        self.stderr = io.StringIO()
        for context in (redirect_stdout(self.stdout), redirect_stderr(self.stderr)):
            context.__enter__()
            self.addCleanup(context.__exit__, None, None, None)
        self.execvp = mock.patch.object(cresume.os, "execvp").start()
        self.addCleanup(mock.patch.stopall)

    def test_missing_sessions_directory(self):
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(self.codex_home / "missing")}):
            self.assertEqual(cresume.main([]), 1)
        self.assertIn("sessions directory not found", self.stderr.getvalue())
        self.execvp.assert_not_called()

    def test_no_sessions_found(self):
        self.assertEqual(cresume.main([]), 1)
        self.assertIn("no user sessions found", self.stderr.getvalue())

    def test_no_cwd_matches(self):
        self.root()
        self.assertEqual(cresume.main(["--cwd"]), 1)
        self.assertIn("no sessions match the current directory", self.stderr.getvalue())

    def test_missing_codex(self):
        self.root()
        with mock.patch.object(cresume.shutil, "which", return_value=None):
            self.assertEqual(cresume.main([]), 1)
        self.assertIn("codex is not in PATH", self.stderr.getvalue())
        self.execvp.assert_not_called()

    def test_missing_fzf_uses_basic_menu_and_execs_codex(self):
        session = self.root()
        with mock.patch.object(cresume.shutil, "which", side_effect=["/bin/codex", None]), mock.patch.object(
            cresume, "choose_basic", return_value=session,
        ) as picker:
            self.assertEqual(cresume.main(["bonsai", "attention"]), 0)
        picker.assert_called_once_with([session], "bonsai attention")
        self.execvp.assert_called_once_with("codex", ["codex", "resume", ROOT_ID])

    def test_fzf_selection_and_cwd_filter(self):
        session = self.root()
        with mock.patch.object(cresume.os, "getcwd", return_value="/code/bonsai/../bonsai"), mock.patch.object(
            cresume.shutil, "which", side_effect=["/bin/codex", "/bin/fzf"],
        ), mock.patch.object(cresume, "choose_with_fzf", return_value=session) as picker:
            self.assertEqual(cresume.main(["--cwd", "bonsai"]), 0)
        picker.assert_called_once_with([session], "bonsai", "/bin/fzf")
        self.execvp.assert_called_once_with("codex", ["codex", "resume", ROOT_ID])

    def test_cancel_does_not_resume(self):
        self.root()
        with mock.patch.object(cresume.shutil, "which", side_effect=["/bin/codex", "/bin/fzf"]), mock.patch.object(
            cresume, "choose_with_fzf", return_value=None,
        ):
            self.assertEqual(cresume.main([]), 0)
        self.execvp.assert_not_called()

    def test_exec_failure_is_short_without_traceback(self):
        session = self.root()
        self.execvp.side_effect = OSError("Codex could not start")
        with mock.patch.object(cresume.shutil, "which", side_effect=["/bin/codex", None]), mock.patch.object(
            cresume, "choose_basic", return_value=session,
        ):
            self.assertEqual(cresume.main([]), 1)
        self.assertIn("Codex could not start", self.stderr.getvalue())
        self.assertNotIn("Traceback", self.stderr.getvalue())

    def test_unset_and_empty_codex_home_default_to_dot_codex(self):
        self.root()
        for value in (None, ""):
            with self.subTest(value=value), mock.patch.dict(os.environ), mock.patch.object(
                cresume.Path, "home", return_value=Path(self.temporary.name),
            ), mock.patch.object(cresume.shutil, "which", side_effect=["/bin/codex", None]), mock.patch.object(
                cresume, "choose_basic", return_value=None,
            ):
                if value is None:
                    os.environ.pop("CODEX_HOME", None)
                else:
                    os.environ["CODEX_HOME"] = value
                self.assertEqual(cresume.main([]), 0)
        self.execvp.assert_not_called()

    def test_help_does_not_require_codex_or_read_sessions(self):
        with mock.patch.object(cresume, "load_sessions") as load, self.assertRaises(SystemExit) as exit_info:
            cresume.main(["--help"])
        self.assertEqual(exit_info.exception.code, 0)
        self.assertIn("--cwd", self.stdout.getvalue())
        load.assert_not_called()


if __name__ == "__main__":
    unittest.main()
