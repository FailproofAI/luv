import tempfile
import unittest
import sys
from pathlib import Path
from unittest.mock import patch

import luv


class AgentLaunchTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.clone_dir = Path(self.tempdir.name)

    def tearDown(self):
        self.tempdir.cleanup()

    def _exec_args(self, func, *args, **kwargs):
        with (patch.object(luv, "trust_project"),
              patch.object(luv, "load_luv_settings", return_value=None),
              patch.object(luv.shutil, "which", side_effect=lambda name: f"/bin/{name}"),
              patch.object(luv.os, "chdir"),
              patch.object(luv.os, "execv") as execv):
            func(*args, **kwargs)
        return execv.call_args.args

    def test_codex_launch_uses_yolo_mode(self):
        executable, argv = self._exec_args(
            luv.launch, self.clone_dir, "fix it", agent="codex")

        self.assertEqual(executable, "/bin/codex")
        self.assertEqual(argv, [
            "/bin/codex",
            "--dangerously-bypass-approvals-and-sandbox",
            "fix it",
        ])

    def test_codex_non_interactive_uses_exec(self):
        _, argv = self._exec_args(
            luv.launch, self.clone_dir, "fix it", agent="codex",
            non_interactive=True, model="gpt-5")

        self.assertEqual(argv, [
            "/bin/codex", "exec",
            "--dangerously-bypass-approvals-and-sandbox",
            "--model", "gpt-5", "fix it",
        ])

    def test_codex_resume_uses_last_session_in_yolo_mode(self):
        _, argv = self._exec_args(
            luv.resume, self.clone_dir, agent="codex")

        self.assertEqual(argv, [
            "/bin/codex", "resume", "--last",
            "--dangerously-bypass-approvals-and-sandbox",
        ])

    def test_claude_remains_default(self):
        _, argv = self._exec_args(luv.launch, self.clone_dir, None)

        self.assertEqual(argv[0], "/bin/claude")
        self.assertIn("claude-opus-4-8", argv)
        self.assertIn("--dangerously-skip-permissions", argv)

    def test_main_routes_codex_to_existing_workspace(self):
        with (patch.object(sys, "argv", ["luv", "--codex", "org/repo", "7"]),
              patch.object(luv, "open_existing") as open_existing):
            luv.main()

        self.assertEqual(open_existing.call_args.kwargs["agent"], "codex")


if __name__ == "__main__":
    unittest.main()
