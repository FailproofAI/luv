import contextlib
import io
import re
import subprocess
import tempfile
import time
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
              patch.object(luv, "tmux_adopt"),
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
        self.assertIn("claude-opus-5", argv)
        self.assertIn("--dangerously-skip-permissions", argv)

    def test_main_routes_codex_to_existing_workspace(self):
        with (patch.object(sys, "argv", ["luv", "--codex", "org/repo", "7"]),
              patch.object(luv, "load_config", return_value={"org": "org"}),
              patch.object(luv, "open_existing") as open_existing):
            luv.main()

        self.assertEqual(open_existing.call_args.kwargs["agent"], "codex")


REMOTE_CONFIG = {
    "org": "exosphere",
    "remote": {
        "host": "box",
        "identity_file": "/keys/box_key",
        "hosts": {"gpu": {"identity_file": "/keys/gpu_key", "port": 2222,
                          "dir": "/scratch/prs"}},
    },
}


class RemoteDispatchTests(unittest.TestCase):
    """The local dispatcher: what argv actually reaches ssh."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.patches = [
            patch.object(luv, "LUV_DIR", root),
            patch.object(luv, "SESSIONS_FILE", root / "sessions.json"),
            patch.object(luv, "SESSIONS_LOCK", root / "sessions.lock"),
            patch.object(luv, "load_config", return_value=REMOTE_CONFIG),
            # These tests call main(), which can reach cmd_clean's rmtree on the
            # local path. Point it at a scratch dir and stub the delete, so a
            # test that stops dispatching can never eat the real ~/prs.
            patch.object(luv, "PRS_DIR", root / "prs"),
            patch.object(luv, "_force_rmtree"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tempdir.cleanup()

    def _dispatch(self, argv, env=None):
        # _LUV_INNER marks a remote-side luv and makes main() refuse to dispatch.
        # It is set in every luv-launched shell — including the one a developer
        # runs these tests from — so clear it or every case below silently
        # becomes a local-execution test.
        env = {"_LUV_INNER": "", **(env or {})}
        with (patch.object(sys, "argv", ["luv"] + argv),
              patch.dict(luv.os.environ, env, clear=False),
              patch.object(luv.shutil, "which", side_effect=lambda n: f"/bin/{n}"),
              patch.object(luv, "hand_over") as hand_over,
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(io.StringIO())):
            luv.main()
        return hand_over.call_args.args[0] if hand_over.called else None

    def test_new_workspace_gets_pending_session(self):
        argv = self._dispatch(["myrepo", "fix it"])

        self.assertEqual(argv[0], "/bin/ssh")
        self.assertIn("-t", argv)
        self.assertIn("-i", argv)
        self.assertIn("/keys/box_key", argv)
        self.assertIn("luv-pending-", argv[-1])
        self.assertIn("_LUV_TMUX_PENDING=", argv[-1])

    def test_reopen_by_number_gets_deterministic_session(self):
        argv = self._dispatch(["myrepo", "42", "keep going"])

        self.assertIn("tmux new-session -A -s luv-myrepo-42", argv[-1])
        self.assertNotIn("_LUV_TMUX_PENDING", argv[-1])

    def test_pr_url_derives_session_from_url(self):
        argv = self._dispatch(["-l", "https://github.com/other/thing/pull/7"])

        self.assertIn("-s luv-thing-7", argv[-1])

    def test_host_flag_selects_per_host_settings(self):
        argv = self._dispatch(["-s", "gpu", "ml", "train"])

        self.assertIn("/keys/gpu_key", argv)
        self.assertIn("2222", argv)
        self.assertIn("gpu", argv)
        self.assertIn("_LUV_PRS_DIR=/scratch/prs", argv[-1])

    def test_identity_flag_overrides_config(self):
        argv = self._dispatch(["-i", "/keys/one_off", "myrepo", "go"])

        self.assertIn("/keys/one_off", argv)
        self.assertNotIn("/keys/box_key", argv)

    def test_identity_file_is_expanded(self):
        base = luv.ssh_base({"host": "box", "identity_file": "~/.ssh/whatever"})

        self.assertNotIn("~/.ssh/whatever", base)
        self.assertTrue(base[base.index("-i") + 1].startswith(str(Path.home())))

    def test_non_interactive_skips_tmux_and_tty(self):
        argv = self._dispatch(["myrepo", "-nit", "summarize"])

        self.assertNotIn("-t", argv)
        self.assertNotIn("tmux", argv[-1])
        self.assertFalse(luv.load_sessions(), "-nit must not record a session")

    def test_clean_dispatches_without_tmux(self):
        argv = self._dispatch(["--clean", "-f"])
        inner = luv.shlex.split(luv.shlex.split(argv[-1])[2])

        self.assertNotIn("tmux", inner)
        self.assertEqual(inner[-3:], ["luv", "--clean", "-f"])

    def test_local_flag_stays_local(self):
        with (patch.object(luv, "open_existing") as open_existing,
              patch.object(sys, "argv", ["luv", "--local", "myrepo", "7"]),
              contextlib.redirect_stdout(io.StringIO())):
            luv.main()

        self.assertTrue(open_existing.called)

    def test_inner_env_never_dispatches_onward(self):
        with (patch.object(luv, "open_existing") as open_existing,
              patch.dict(luv.os.environ, {"_LUV_INNER": "1"}),
              patch.object(sys, "argv", ["luv", "myrepo", "7"]),
              contextlib.redirect_stdout(io.StringIO())):
            luv.main()

        self.assertTrue(open_existing.called)

    def test_org_is_resolved_locally(self):
        argv = self._dispatch(["myrepo", "go"])

        self.assertIn("luv exosphere/myrepo", argv[-1])

    def test_prompt_quoting_survives_the_remote_shell(self):
        prompt = """fix $HOME's "quoted" `thing` && rm -rf /"""
        argv = self._dispatch(["myrepo", prompt])

        # Undo the two layers of quoting the way the remote shells will.
        outer = luv.shlex.split(argv[-1])          # remote login shell
        self.assertEqual(outer[:2], ["bash", "-lc"])
        self.assertEqual(luv.shlex.split(outer[2])[-1], prompt)

    def test_local_flag_rejects_ssh_flags(self):
        with self.assertRaises(SystemExit):
            self._dispatch(["--local", "-s", "gpu", "myrepo"])


class _FakeProc:
    """A child that can raise KeyboardInterrupt before it finally exits."""

    def __init__(self, returncode=0, interrupts=0):
        self.returncode = returncode
        self.interrupts = interrupts

    def wait(self):
        if self.interrupts:
            self.interrupts -= 1
            raise KeyboardInterrupt
        return self.returncode


class TerminalRestoreTests(unittest.TestCase):
    """A connection that dies must not leave the terminal in the remote
    program's modes — that's the "35;22;1M" junk at the shell prompt."""

    FD = 42
    SAVED = ["saved", "termios", "attrs"]

    def _hand_over(self, argv, returncode=0, interrupts=0, **kwargs):
        proc = _FakeProc(returncode, interrupts)
        self.writes = []
        with (patch.object(luv, "terminal_fd", return_value=self.FD),
              patch.object(luv.subprocess, "Popen", return_value=proc) as popen,
              patch.object(luv.termios, "tcgetattr", return_value=self.SAVED),
              patch.object(luv.termios, "tcsetattr") as tcsetattr,
              patch.object(luv.os, "execv") as execv,
              patch.object(luv.os, "write",
                           side_effect=lambda fd, data: self.writes.append((fd, data))),
              self.assertRaises(SystemExit) as exit_ctx):
            luv.hand_over(argv, **kwargs)
        self.popen, self.tcsetattr, self.execv = popen, tcsetattr, execv
        return exit_ctx.exception.code

    def _reset_bytes(self):
        return b"".join(data for fd, data in self.writes if fd == self.FD)

    def test_broken_connection_still_restores_the_terminal(self):
        # 255 is what ssh exits with when the connection drops under it.
        code = self._hand_over(["/bin/ssh", "box", "tmux attach"], returncode=255)

        self.assertEqual(code, 255, "the child's exit code must still pass through")
        self.assertTrue(self.popen.called, "restore mode must not exec the child away")
        self.assertIn(b"\x1b[?1003l", self._reset_bytes(), "mouse tracking left on")
        self.assertIn(b"\x1b[?1006l", self._reset_bytes(), "SGR mouse reports left on")
        self.assertIn(b"\x1b[?2004l", self._reset_bytes(), "bracketed paste left on")
        self.assertIn(b"\x1b[?1049l", self._reset_bytes(), "alternate screen left on")
        self.assertEqual(self.tcsetattr.call_args.args[0], self.FD)
        self.assertEqual(self.tcsetattr.call_args.args[2], self.SAVED)

    def test_clean_exit_restores_too(self):
        code = self._hand_over(["/bin/tmux", "attach"], returncode=0)

        self.assertEqual(code, 0)
        self.assertIn(b"\x1b[?1003l", self._reset_bytes())

    def test_ctrl_c_does_not_kill_the_parent_before_cleanup(self):
        # Ctrl-C reaches the child through the tty; the parent must outlive it
        # or there is nobody left to clean up after it.
        code = self._hand_over(["/bin/ssh", "box"], returncode=130, interrupts=2)

        self.assertEqual(code, 130)
        self.assertIn(b"\x1b[?1003l", self._reset_bytes())

    def test_no_tty_handoff_still_execs(self):
        # -nit streams stream-json into a pipe: no terminal to restore, so keep
        # the cheaper exec and don't leave a process in the middle.
        writes = []
        with (patch.object(luv, "terminal_fd", return_value=self.FD),
              patch.object(luv.os, "execv") as execv,
              patch.object(luv.os, "write", side_effect=writes.append),
              patch.object(luv.subprocess, "Popen") as popen):
            luv.hand_over(["/bin/ssh", "box"], restore=False)

        self.assertEqual(execv.call_args.args, ("/bin/ssh", ["/bin/ssh", "box"]))
        self.assertFalse(popen.called)
        self.assertEqual(writes, [])

    def test_guard_is_a_noop_without_a_terminal(self):
        with (patch.object(luv, "terminal_fd", return_value=None),
              patch.object(luv.os, "write") as write,
              patch.object(luv.termios, "tcsetattr") as tcsetattr):
            with luv.terminal_guard():
                pass

        self.assertFalse(write.called)
        self.assertFalse(tcsetattr.called)

    def test_restore_survives_a_child_that_never_started(self):
        # An OSError out of Popen must not skip the cleanup either.
        writes = []
        with (patch.object(luv, "terminal_fd", return_value=self.FD),
              patch.object(luv.termios, "tcgetattr", return_value=self.SAVED),
              patch.object(luv.termios, "tcsetattr"),
              patch.object(luv.os, "write",
                           side_effect=lambda fd, data: writes.append(data)),
              patch.object(luv.subprocess, "Popen", side_effect=OSError("boom"))):
            with self.assertRaises(OSError):
                luv.hand_over(["/bin/ssh", "box"])

        self.assertIn(b"\x1b[?1003l", b"".join(writes))

    def test_local_attach_goes_through_the_guard(self):
        with (patch.object(luv.shutil, "which", side_effect=lambda n: f"/bin/{n}"),
              patch.object(luv, "hand_over") as hand_over):
            luv.attach_session(None, "luv-myrepo-42")

        self.assertEqual(hand_over.call_args.args[0],
                         ["/bin/tmux", "attach", "-d", "-t", "luv-myrepo-42"])

    def test_remote_attach_asks_for_a_tty_and_restores(self):
        with (patch.object(luv.shutil, "which", side_effect=lambda n: f"/bin/{n}"),
              patch.object(luv, "hand_over") as hand_over,
              contextlib.redirect_stdout(io.StringIO())):
            luv.attach_session({"host": "box"}, "luv-myrepo-42")

        argv = hand_over.call_args.args[0]
        self.assertIn("-t", argv)
        self.assertNotEqual(hand_over.call_args.kwargs.get("restore"), False)


class SessionNameTests(unittest.TestCase):
    def test_illegal_tmux_characters_are_replaced(self):
        self.assertEqual(luv.tmux_session_name("foo.js-7"), "luv-foo_js-7")
        self.assertEqual(luv.tmux_session_name("a:b-1"), "luv-a_b-1")

    def test_plain_names_are_untouched(self):
        self.assertEqual(luv.tmux_session_name("myrepo-42"), "luv-myrepo-42")


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class RegistryTests(unittest.TestCase):
    """Reconciliation must never lose a session just because a host is down."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.patches = [
            patch.object(luv, "LUV_DIR", root),
            patch.object(luv, "SESSIONS_FILE", root / "sessions.json"),
            patch.object(luv, "SESSIONS_LOCK", root / "sessions.lock"),
            patch.object(luv, "load_config", return_value=REMOTE_CONFIG),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tempdir.cleanup()

    def test_renamed_session_is_matched_by_luv_id(self):
        entry = {"id": "abc123", "host": "box", "session": "luv-pending-abc123",
                 "workspace": None, "repo": "myrepo"}
        line = "abc123|luv-myrepo-42|myrepo-42|1|1700000000\n"

        with patch.object(luv, "ssh_run", return_value=_completed(line)):
            kept, unreachable = luv.reconcile([entry])

        self.assertEqual(unreachable, set())
        self.assertEqual(kept[0]["session"], "luv-myrepo-42")
        self.assertEqual(kept[0]["workspace"], "myrepo-42")
        self.assertTrue(kept[0]["attached"])
        self.assertTrue(kept[0]["live"])

    def test_dead_session_is_pruned_when_host_answers(self):
        entry = {"id": "abc123", "host": "box", "session": "luv-myrepo-42"}

        with patch.object(luv, "ssh_run", return_value=_completed("")):
            kept, unreachable = luv.reconcile([entry])

        self.assertEqual(kept, [])
        self.assertEqual(unreachable, set())

    def test_unreachable_host_keeps_its_entries(self):
        entry = {"id": "abc123", "host": "box", "session": "luv-myrepo-42"}

        with patch.object(luv, "ssh_run",
                          return_value=_completed("", 255, "ssh: connect: timed out")):
            kept, unreachable = luv.reconcile([entry])

        self.assertEqual(len(kept), 1, "an offline host must not wipe the registry")
        self.assertIsNone(kept[0]["live"])
        self.assertEqual(unreachable, {"box"})

    def test_unstamped_session_falls_back_to_name_match(self):
        # A session whose remote luv died before tmux_adopt ran has no @luv_id,
        # which tmux renders as an empty field.
        entry = {"id": "abc123", "host": "box", "session": "luv-myrepo-42"}
        line = "|luv-myrepo-42|myrepo-42|0|1700000000\n"

        with patch.object(luv, "ssh_run", return_value=_completed(line)):
            kept, _ = luv.reconcile([entry])

        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0]["live"])

    def test_sessions_without_the_luv_prefix_are_ignored(self):
        entry = {"id": "abc123", "host": "box", "session": "luv-myrepo-42"}
        line = "|someones-other-session||0|1700000000\n"

        with patch.object(luv, "ssh_run", return_value=_completed(line)):
            kept, _ = luv.reconcile([entry])

        self.assertEqual(kept, [])

    def test_transient_fields_are_not_persisted(self):
        luv.save_sessions([{"id": "a", "live": True, "attached": True, "activity": 9}])
        stored = luv.load_sessions()[0]

        self.assertEqual(stored, {"id": "a"})

    def test_record_session_appends_and_replaces_by_id(self):
        luv.record_session({"id": "a", "host": "box"})
        luv.record_session({"id": "b", "host": "box"})
        luv.record_session({"id": "a", "host": "gpu"})
        stored = luv.load_sessions()

        self.assertEqual(len(stored), 2)
        self.assertEqual({s["id"]: s["host"] for s in stored}, {"a": "gpu", "b": "box"})

    def test_corrupt_registry_is_ignored(self):
        luv.SESSIONS_FILE.write_text("{not json")

        self.assertEqual(luv.load_sessions(), [])


class TmuxAdoptTests(unittest.TestCase):
    """The remote side: rename the placeholder, stamp identity onto the session."""

    def test_noop_outside_tmux(self):
        with (patch.dict(luv.os.environ, {}, clear=True),
              patch.object(luv, "run") as run):
            luv.tmux_adopt(Path("/tmp/myrepo-42"))

        self.assertFalse(run.called)

    def test_renames_placeholder_and_stamps_identity(self):
        env = {"TMUX": "/tmp/tmux-1000/default,1,0",
               "_LUV_TMUX_PENDING": "luv-pending-abc", "_LUV_ID": "abc"}
        with (patch.dict(luv.os.environ, env, clear=True),
              patch.object(luv, "run", return_value=_completed()) as run):
            luv.tmux_adopt(Path("/tmp/myrepo-42"))

        calls = [c.args[0] for c in run.call_args_list]
        self.assertIn(["tmux", "rename-session", "-t", "luv-pending-abc",
                       "luv-myrepo-42"], calls)
        self.assertIn(["tmux", "set-option", "-t", "luv-myrepo-42",
                       "@luv_workspace", "myrepo-42"], calls)
        self.assertIn(["tmux", "set-option", "-t", "luv-myrepo-42",
                       "@luv_id", "abc"], calls)

    def test_name_collision_falls_back_to_suffix(self):
        env = {"TMUX": "x", "_LUV_TMUX_PENDING": "luv-pending-abc"}
        with (patch.dict(luv.os.environ, env, clear=True),
              patch.object(luv, "run", side_effect=[_completed("", 1), _completed(),
                                                    _completed()]) as run,
              contextlib.redirect_stderr(io.StringIO())):
            luv.tmux_adopt(Path("/tmp/myrepo-42"))

        calls = [c.args[0] for c in run.call_args_list]
        self.assertEqual(calls[1][-1], "luv-myrepo-42-2")
        self.assertEqual(calls[2][3], "luv-myrepo-42-2")

    def test_deterministic_path_still_stamps(self):
        with (patch.dict(luv.os.environ, {"TMUX": "x", "_LUV_ID": "abc"}, clear=True),
              patch.object(luv, "run", return_value=_completed()) as run):
            luv.tmux_adopt(Path("/tmp/myrepo-42"))

        calls = [c.args[0] for c in run.call_args_list]
        self.assertNotIn("rename-session", [c[1] for c in calls])
        self.assertEqual(len(calls), 2)


class ConfigTests(unittest.TestCase):
    def test_dotted_keys_round_trip(self):
        data = {}
        luv.config_set(data, "remote.hosts.gpu.port", 2222)

        self.assertEqual(data, {"remote": {"hosts": {"gpu": {"port": 2222}}}})
        self.assertEqual(luv.config_get(data, "remote.hosts.gpu.port"), 2222)
        self.assertIs(luv.config_get(data, "remote.missing"), luv._MISSING)
        self.assertTrue(luv.config_unset(data, "remote.hosts.gpu.port"))
        self.assertFalse(luv.config_unset(data, "remote.hosts.gpu.port"))

    def test_flatten_sorts_and_dots(self):
        data = {"org": "x", "remote": {"host": "box", "port": 22}}

        self.assertEqual(luv.config_flatten(data),
                         [("org", "x"), ("remote.host", "box"), ("remote.port", 22)])

    def test_per_host_overrides_beat_defaults(self):
        with patch.object(luv, "load_config", return_value=REMOTE_CONFIG):
            hc = luv.resolve_host("gpu")

        self.assertEqual(hc["identity_file"], "/keys/gpu_key")
        self.assertEqual(hc["port"], 2222)

    def test_default_host_used_when_no_flag(self):
        with patch.object(luv, "load_config", return_value=REMOTE_CONFIG):
            hc = luv.resolve_host()

        self.assertEqual(hc["host"], "box")
        self.assertEqual(hc["identity_file"], "/keys/box_key")

    def test_no_remote_configured_is_local(self):
        with patch.object(luv, "load_config", return_value={"org": "x"}):
            self.assertIsNone(luv.resolve_host())

    def test_batch_mode_adds_timeouts(self):
        base = luv.ssh_base({"host": "box"}, batch=True)

        self.assertIn("BatchMode=yes", base)
        self.assertIn("ConnectTimeout=5", base)


class CleanGuardTests(unittest.TestCase):
    def test_live_session_folder_is_skipped(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        (root / "myrepo-42").mkdir()

        out = io.StringIO()
        with (patch.object(luv, "PRS_DIR", root),
              patch.object(luv, "live_tmux_sessions", return_value={"luv-myrepo-42"}),
              patch.object(luv, "_force_rmtree") as rmtree,
              contextlib.redirect_stdout(out)):
            luv.cmd_clean(force=False)

        self.assertFalse(rmtree.called)
        self.assertIn("live tmux session", out.getvalue())


class WorkspaceNumberTests(unittest.TestCase):
    def test_parses_trailing_number(self):
        self.assertEqual(luv.workspace_number("myrepo-42"), 42)

    def test_handles_repos_with_hyphens(self):
        self.assertEqual(luv.workspace_number("my-cool-repo-7"), 7)

    def test_rejects_non_workspace_names(self):
        self.assertIsNone(luv.workspace_number("myrepo"))
        self.assertIsNone(luv.workspace_number("myrepo-main"))
        self.assertIsNone(luv.workspace_number(None))


class PrLinkTests(unittest.TestCase):
    """The PR column: authoritative, cached, and never blocking on gh."""

    def setUp(self):
        self.which = patch.object(luv.shutil, "which",
                                  side_effect=lambda name: f"/bin/{name}")
        self.which.start()
        self.addCleanup(self.which.stop)

    @staticmethod
    def _session(**over):
        entry = {"id": "abc123", "org": "acme", "repo": "myrepo",
                 "workspace": "myrepo-42"}
        entry.update(over)
        return entry

    _PR_JSON = '[{"number": 42, "url": "https://github.com/acme/myrepo/pull/42"}]'

    def test_head_query_populates_the_link(self):
        rows = [self._session()]

        with patch.object(luv, "run", return_value=_completed(self._PR_JSON)) as run:
            self.assertTrue(luv.attach_pr_links(rows))

        self.assertEqual(rows[0]["pr_number"], 42)
        self.assertEqual(rows[0]["pr_url"], "https://github.com/acme/myrepo/pull/42")
        self.assertIn("luv-42", run.call_args.args[0])

    def test_branch_is_queried_without_an_owner_prefix(self):
        # REST's head= filter needs {owner}:{branch}, and the owner luv recorded
        # stops matching once the org is renamed. gh pr list takes a bare branch.
        rows = [self._session()]

        with patch.object(luv, "run", return_value=_completed(self._PR_JSON)) as run:
            luv.attach_pr_links(rows)

        argv = run.call_args.args[0]
        self.assertEqual(argv[:3], ["gh", "pr", "list"])
        self.assertEqual(argv[argv.index("--head") + 1], "luv-42")
        self.assertNotIn("acme:luv-42", argv)

    def test_cached_result_is_not_refetched(self):
        rows = [self._session(pr_number=42, pr_url="https://x/42",
                              pr_checked=int(time.time()))]

        with patch.object(luv, "run") as run:
            self.assertFalse(luv.attach_pr_links(rows))

        self.assertFalse(run.called, "a fresh cache entry must not hit the network")

    def test_stale_cache_is_refetched(self):
        rows = [self._session(pr_url="https://x/42",
                              pr_checked=int(time.time()) - luv.PR_TTL_OK - 1)]

        with patch.object(luv, "run", return_value=_completed(self._PR_JSON)) as run:
            luv.attach_pr_links(rows)

        self.assertTrue(run.called)

    def test_missing_pr_is_cached_briefly(self):
        # No PR yet is the volatile case: re-ask sooner than for one we found.
        rows = [self._session(pr_checked=int(time.time()) - 1)]

        with patch.object(luv, "run") as run:
            luv.attach_pr_links(rows)
        self.assertFalse(run.called)

        rows[0]["pr_checked"] = int(time.time()) - luv.PR_TTL_MISS - 1
        with patch.object(luv, "run", return_value=_completed("[]")) as run:
            luv.attach_pr_links(rows)
        self.assertTrue(run.called)
        self.assertIsNone(rows[0]["pr_url"])

    def test_pr_hint_resolves_without_a_network_call(self):
        # -l / -pr sessions: the branch is the PR's own head ref, so the head
        # query would never find it — but the number is already known.
        rows = [self._session(workspace="myrepo-123", pr_hint=123)]

        with patch.object(luv, "run") as run:
            luv.attach_pr_links(rows)

        self.assertFalse(run.called)
        self.assertEqual(rows[0]["pr_url"], "https://github.com/acme/myrepo/pull/123")

    def test_sessions_without_a_workspace_number_are_skipped(self):
        rows = [self._session(workspace=None), self._session(org=None)]

        with patch.object(luv, "run") as run:
            self.assertFalse(luv.attach_pr_links(rows))

        self.assertFalse(run.called)
        self.assertNotIn("pr_url", rows[0])

    def test_missing_gh_keeps_the_cached_link(self):
        rows = [self._session(pr_number=42, pr_url="https://x/42", pr_checked=0)]
        err = io.StringIO()

        with (patch.object(luv.shutil, "which", return_value=None),
              patch.object(luv, "run") as run,
              contextlib.redirect_stderr(err)):
            luv.attach_pr_links(rows)

        self.assertFalse(run.called)
        self.assertEqual(rows[0]["pr_url"], "https://x/42")
        self.assertIn("gh", err.getvalue())

    def test_gh_failure_leaves_no_link(self):
        rows = [self._session()]

        with patch.object(luv, "run", return_value=_completed("", 1, "boom")):
            luv.attach_pr_links(rows)

        self.assertIsNone(rows[0]["pr_url"])

    def test_timeout_is_a_failure_not_an_exception(self):
        with patch.object(luv.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("gh", 10)):
            r = luv.run(["gh", "api", "whatever"], timeout=10)

        self.assertNotEqual(r.returncode, 0)


class RmWorkspaceTests(unittest.TestCase):
    """`luv rm` runs rm -rf over ssh. The guardrails are the feature."""

    def test_refuses_a_name_that_is_not_a_workspace(self):
        for bad in ("../../etc", "myrepo", "", "*"):
            with patch.object(luv, "ssh_run") as ssh:
                err = luv.rm_workspace({"host": "box"}, "luv-x", bad)
            self.assertIsNotNone(err, f"{bad!r} must be refused")
            self.assertFalse(ssh.called, f"{bad!r} must not reach the remote")

    def test_kills_tmux_then_deletes_the_folder(self):
        with patch.object(luv, "ssh_run", return_value=_completed()) as ssh:
            err = luv.rm_workspace({"host": "box"}, "luv-myrepo-42", "myrepo-42")

        self.assertIsNone(err)
        cmd = ssh.call_args.args[1]
        self.assertIn("tmux kill-session -t luv-myrepo-42", cmd)
        self.assertIn('rm -rf -- "$HOME/prs"/myrepo-42', cmd)
        self.assertLess(cmd.index("kill-session"), cmd.index("rm -rf"))

    def test_home_is_expanded_on_the_remote_not_here(self):
        # The laptop's home directory is not the box's.
        with patch.object(luv, "ssh_run", return_value=_completed()) as ssh:
            luv.rm_workspace({"host": "box"}, None, "myrepo-42")

        self.assertIn('"$HOME/prs"', ssh.call_args.args[1])

    def test_uses_the_hosts_configured_dir(self):
        with patch.object(luv, "ssh_run", return_value=_completed()) as ssh:
            luv.rm_workspace({"host": "gpu", "dir": "/scratch/prs"}, None, "myrepo-42")

        self.assertIn("rm -rf -- /scratch/prs/myrepo-42", ssh.call_args.args[1])

    def test_unreachable_host_is_reported_not_swallowed(self):
        with patch.object(luv, "ssh_run", return_value=_completed("", 255)):
            err = luv.rm_workspace({"host": "box"}, "luv-myrepo-42", "myrepo-42")

        self.assertEqual(err, "host unreachable")

    def test_orphans_are_folders_with_no_live_session(self):
        live = "abc|luv-myrepo-42|myrepo-42|0|1700000000\n"
        listing = "myrepo-42\nmyrepo-9\nnotes\n"

        with patch.object(luv, "ssh_run",
                          side_effect=[_completed(live), _completed(listing)]):
            orphans = luv.orphan_workspaces({"host": "box"})

        self.assertEqual(orphans, ["myrepo-9"], "live folder and non-workspace kept")

    def test_unreachable_host_yields_no_orphans(self):
        with patch.object(luv, "ssh_run", return_value=_completed("", 255)):
            self.assertIsNone(luv.orphan_workspaces({"host": "box"}))


class CmdRmTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        for p in (patch.object(luv, "LUV_DIR", root),
                  patch.object(luv, "SESSIONS_FILE", root / "sessions.json"),
                  patch.object(luv, "SESSIONS_LOCK", root / "sessions.lock"),
                  patch.object(luv, "load_config", return_value=REMOTE_CONFIG)):
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def _entry(**over):
        e = {"id": "a", "host": "box", "session": "luv-myrepo-42",
             "workspace": "myrepo-42", "org": "acme", "repo": "myrepo"}
        e.update(over)
        return e

    @contextlib.contextmanager
    def _run_rm(self, rows, answer=None):
        """Drive cmd_rm with a fixed registry and a stubbed remote."""
        with (patch.object(luv, "refresh_sessions", return_value=(rows, set())),
              patch.object(luv, "attach_pr_links", return_value=False),
              # No test here may reach a real host: the folder-scan fallback
              # would otherwise ssh to whatever REMOTE_CONFIG names.
              patch.object(luv, "workspace_exists", return_value=False),
              patch.object(luv, "rm_workspace", return_value=None) as rm,
              patch("builtins.input", return_value=answer or ""),
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(io.StringIO())):
            yield rm

    def test_named_target_removes_and_forgets_the_entry(self):
        luv.save_sessions([self._entry()])

        with self._run_rm([self._entry()]) as rm:
            luv.cmd_rm(["myrepo-42"])

        self.assertEqual(rm.call_args.args[2], "myrepo-42")
        self.assertEqual(luv.load_sessions(), [], "registry entry must be dropped")

    def test_session_name_also_matches(self):
        with self._run_rm([self._entry()]) as rm:
            luv.cmd_rm(["luv-myrepo-42"])

        self.assertTrue(rm.called)

    def test_named_target_needs_no_confirmation(self):
        with self._run_rm([self._entry()]) as rm:
            with patch("builtins.input", side_effect=AssertionError("must not prompt")):
                luv.cmd_rm(["myrepo-42"])

        self.assertTrue(rm.called)

    def test_a_finished_session_is_found_by_folder_scan(self):
        # Reconciliation already dropped the entry, which is precisely the case
        # you reach for `luv rm` in. The folder is still on disk.
        with (patch.object(luv, "refresh_sessions", return_value=([], set())),
              patch.object(luv, "workspace_exists", side_effect=lambda hc, w: hc is None),
              patch.object(luv, "rm_workspace", return_value=None) as rm,
              contextlib.redirect_stdout(io.StringIO())):
            luv.cmd_rm(["myrepo-42"])

        self.assertEqual(rm.call_args.args[2], "myrepo-42")
        self.assertEqual(rm.call_args.args[1], "luv-myrepo-42", "kill its tmux too")

    def test_session_name_form_resolves_to_the_folder(self):
        # 'luv-myrepo-2' parses as a {repo}-{N} in its own right, so the luv-
        # prefix has to be tried as a fallback rather than stripped up front.
        exists = lambda hc, w: hc is None and w == "myrepo-2"

        with (patch.object(luv, "refresh_sessions", return_value=([], set())),
              patch.object(luv, "workspace_exists", side_effect=exists),
              patch.object(luv, "rm_workspace", return_value=None) as rm,
              contextlib.redirect_stdout(io.StringIO())):
            luv.cmd_rm(["luv-myrepo-2"])

        self.assertEqual(rm.call_args.args[2], "myrepo-2")

    def test_a_folder_that_is_not_a_workspace_is_never_targeted(self):
        # `test -d ~/prs/notes` succeeds; that must not make it a target.
        with (patch.object(luv, "refresh_sessions", return_value=([], set())),
              patch.object(luv, "workspace_exists", return_value=True),
              patch.object(luv, "rm_workspace") as rm,
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(io.StringIO())):
            with self.assertRaises(SystemExit):
                luv.cmd_rm(["notes"])

        self.assertFalse(rm.called)

    def test_unknown_target_is_an_error_and_removes_nothing(self):
        with self._run_rm([]) as rm:
            with self.assertRaises(SystemExit):
                luv.cmd_rm(["myrepo-42"])

        self.assertFalse(rm.called)

    def test_a_failed_delete_keeps_the_registry_entry(self):
        luv.save_sessions([self._entry()])

        with (patch.object(luv, "refresh_sessions", return_value=([self._entry()], set())),
              patch.object(luv, "rm_workspace", return_value="host unreachable"),
              contextlib.redirect_stdout(io.StringIO())):
            luv.cmd_rm(["myrepo-42"])

        self.assertEqual(len(luv.load_sessions()), 1, "a failed delete must not forget")

    def test_merged_selects_only_merged_prs(self):
        rows = [self._entry(id="a", workspace="myrepo-1", pr_state="MERGED"),
                self._entry(id="b", workspace="myrepo-2", pr_state="OPEN"),
                self._entry(id="c", workspace="myrepo-3", pr_state=None)]

        with self._run_rm(rows, answer="y") as rm:
            luv.cmd_rm(["--merged"])

        self.assertEqual([c.args[2] for c in rm.call_args_list], ["myrepo-1"])

    def test_bulk_removal_aborts_unless_confirmed(self):
        rows = [self._entry(workspace="myrepo-1", pr_state="MERGED")]

        with self._run_rm(rows, answer="n") as rm:
            luv.cmd_rm(["--merged"])

        self.assertFalse(rm.called, "a bare Enter must not delete anything")

    def test_force_skips_the_prompt(self):
        rows = [self._entry(workspace="myrepo-1", pr_state="MERGED")]

        with self._run_rm(rows) as rm:
            with patch("builtins.input", side_effect=AssertionError("must not prompt")):
                luv.cmd_rm(["--merged"], force=True)

        self.assertTrue(rm.called)

    def test_dead_skips_an_unreachable_host(self):
        with (patch.object(luv, "refresh_sessions", return_value=([self._entry()], set())),
              patch.object(luv, "orphan_workspaces", return_value=None),
              patch.object(luv, "rm_workspace") as rm,
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(io.StringIO())):
            luv.cmd_rm(["--dead"], force=True)

        self.assertFalse(rm.called, "an offline box must not be treated as empty")

    def test_dead_removes_orphaned_folders(self):
        with (patch.object(luv, "refresh_sessions", return_value=([self._entry()], set())),
              patch.object(luv, "orphan_workspaces", return_value=["myrepo-9"]),
              patch.object(luv, "rm_workspace", return_value=None) as rm,
              contextlib.redirect_stdout(io.StringIO())):
            luv.cmd_rm(["--dead"], force=True)

        self.assertEqual(rm.call_args.args[2], "myrepo-9")
        self.assertIsNone(rm.call_args.args[1], "an orphan has no session to kill")

    def test_host_filter_scopes_the_selection(self):
        rows = [self._entry(id="a", host="box", workspace="myrepo-1", pr_state="MERGED"),
                self._entry(id="b", host="gpu", workspace="myrepo-2", pr_state="MERGED")]

        with self._run_rm(rows, answer="y") as rm:
            luv.cmd_rm(["--merged", "--host", "gpu"])

        self.assertEqual([c.args[2] for c in rm.call_args_list], ["myrepo-2"])

    def test_no_selector_and_no_target_is_an_error(self):
        with self._run_rm([self._entry()]) as rm:
            with self.assertRaises(SystemExit):
                luv.cmd_rm([])

        self.assertFalse(rm.called)


class SessionTableTests(unittest.TestCase):
    """The link has to survive both a terminal and a pipe."""

    ROWS = [
        {"host": "box", "session": "luv-myrepo-42", "workspace": "myrepo-42",
         "agent": "claude", "live": True, "attached": True, "prompt": "fix it",
         "pr_number": 42, "pr_url": "https://github.com/acme/myrepo/pull/42"},
        {"host": "box", "session": "luv-myrepo-51", "workspace": "myrepo-51",
         "agent": "codex", "live": True, "attached": False, "prompt": "add limits"},
    ]

    def _render(self, tty):
        out = io.StringIO()
        out.isatty = lambda: tty
        with contextlib.redirect_stdout(out):
            luv.print_sessions(self.ROWS)
        return out.getvalue().splitlines()

    def test_piped_output_shows_the_full_url(self):
        lines = self._render(tty=False)

        self.assertIn("PR", lines[0])
        self.assertIn("https://github.com/acme/myrepo/pull/42", lines[1])
        self.assertNotIn("\033", lines[1], "escapes would corrupt a redirect")

    def test_terminal_output_hyperlinks_a_short_number(self):
        lines = self._render(tty=True)

        self.assertIn("\033]8;;https://github.com/acme/myrepo/pull/42\033\\#42", lines[1])

    def test_columns_stay_aligned_around_the_escape(self):
        # The link's escape bytes must not enter the width arithmetic: the
        # PROMPT column has to start at the same offset on every row.
        lines = self._render(tty=True)
        visible = [re.sub(r"\033]8;;[^\033]*\033\\", "", line) for line in lines]

        self.assertEqual(visible[0].index("PROMPT"), visible[1].index("fix it"))
        self.assertEqual(visible[1].index("fix it"), visible[2].index("add limits"))

    def test_session_without_a_pr_shows_a_dash(self):
        lines = self._render(tty=True)

        self.assertRegex(lines[2], r"\s-\s+add limits$")


if __name__ == "__main__":
    unittest.main()
