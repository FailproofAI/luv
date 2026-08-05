import contextlib
import io
import json
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

    def _dispatch(self, argv, env=None, where=None):
        """Dispatch and return the argv handed to ssh.

        `where` is what the remote's 'luv --where' answers; None makes the host
        look unreachable, which is what drives the luv-pending fallback.

        _LUV_INNER marks a remote-side luv and makes main() refuse to dispatch.
        It is set in every luv-launched shell — including the one a developer
        runs these tests from — so clear it or every case below silently becomes
        a local-execution test.
        """
        answer = _completed(f"{where}\n") if where else _completed(returncode=255)
        env = {"_LUV_INNER": "", **(env or {})}
        with (patch.object(sys, "argv", ["luv"] + argv),
              patch.dict(luv.os.environ, env, clear=False),
              patch.object(luv, "ssh_run", return_value=answer),
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
        argv = self._dispatch(["myrepo", "42", "keep going"], where="myrepo-box-42")

        self.assertIn("tmux new-session -A -s luv-myrepo-box-42", argv[-1])
        self.assertNotIn("_LUV_TMUX_PENDING", argv[-1])

    def test_reopen_falls_back_to_pending_when_host_is_silent(self):
        # A folder name we cannot resolve must not become a guess: the remote
        # renames the pending session once it knows.
        argv = self._dispatch(["myrepo", "42", "keep going"])

        self.assertIn("luv-pending-", argv[-1])
        self.assertIn("_LUV_TMUX_PENDING=", argv[-1])

    def test_reopen_by_number_prefers_the_registry_over_a_round_trip(self):
        luv.record_session({"id": "abc", "host": "box", "repo": "myrepo",
                            "workspace": "myrepo-mbp-42",
                            "session": "luv-myrepo-mbp-42"})

        argv = self._dispatch(["myrepo", "42"], where="myrepo-box-42")

        self.assertIn("tmux new-session -A -s luv-myrepo-mbp-42", argv[-1])

    def test_pr_url_gets_a_pending_session(self):
        # -l clones a fresh folder on the remote, so its name — and with it the
        # session name — is not knowable here even when the host answers.
        argv = self._dispatch(["-l", "https://github.com/other/thing/pull/7"],
                              where="thing-box-7")

        self.assertIn("luv-pending-", argv[-1])
        self.assertIn("_LUV_TMUX_PENDING=", argv[-1])

    def test_pr_url_with_resume_pins_the_existing_folder(self):
        # -r reopens the newest folder that is already there, so it can attach.
        argv = self._dispatch(["-l", "https://github.com/other/thing/pull/7", "-r"],
                              where="thing-box-7")

        self.assertIn("-s luv-thing-box-7", argv[-1])
        self.assertNotIn("_LUV_TMUX_PENDING", argv[-1])

    def test_pr_url_still_records_the_pr_number(self):
        # The PR link in `luv ls` comes from this hint: a -l session's branch is
        # the PR's head ref, which no folder name spells out.
        self._dispatch(["-l", "https://github.com/other/thing/pull/7"])

        self.assertEqual([s.get("pr_hint") for s in luv.load_sessions()], [7])

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


class _OrderedStderr(io.StringIO):
    """A stderr that notes where its first write falls in a sequence."""

    def __init__(self, order):
        super().__init__()
        self.order = order

    def write(self, s):
        if s.strip() and "hint" not in self.order:
            self.order.append("hint")
        return super().write(s)


class ContinueHintTests(unittest.TestCase):
    """A session that breaks must say how to get back into it, in one line
    that can be copied off the terminal and run as-is."""

    FD = TerminalRestoreTests.FD
    SAVED = TerminalRestoreTests.SAVED
    HINT = "luv continue myrepo 42"

    def _hand_over(self, stderr, **kwargs):
        """Run hand_over over a fake child, with the terminal all stubbed out."""
        with (patch.object(luv, "terminal_fd", return_value=self.FD),
              patch.object(luv.subprocess, "Popen",
                           return_value=_FakeProc(kwargs.pop("returncode", 0))),
              patch.object(luv.termios, "tcgetattr", return_value=self.SAVED),
              patch.object(luv.termios, "tcsetattr"),
              patch.object(luv.os, "write", side_effect=self.on_write),
              contextlib.redirect_stderr(stderr),
              self.assertRaises(SystemExit) as exit_ctx):
            luv.hand_over(["/bin/ssh", "box"], **kwargs)
        return exit_ctx.exception.code

    def on_write(self, fd, data):
        return len(data)

    def test_broken_session_hands_back_a_runnable_command(self):
        err = io.StringIO()
        code = self._hand_over(err, returncode=255, hint=self.HINT)

        self.assertEqual(code, 255)
        self.assertIn(self.HINT, err.getvalue())
        # Copy-pasteable means the whole command sits on a line of its own, with
        # nothing but indentation around it to select past.
        line = next(l for l in err.getvalue().splitlines() if self.HINT in l)
        self.assertEqual(line.strip(), self.HINT)

    def test_a_clean_exit_says_nothing(self):
        # Detaching and the agent finishing both land here; neither is broken.
        err = io.StringIO()
        code = self._hand_over(err, returncode=0, hint=self.HINT)

        self.assertEqual(code, 0)
        self.assertNotIn("luv continue", err.getvalue())

    def test_the_hint_waits_for_the_terminal_to_be_restored(self):
        # Printed inside the guard it would land in the remote program's modes,
        # which is where unreadable output comes from in the first place.
        order = []
        self.on_write = lambda fd, data: order.append("reset")
        self._hand_over(_OrderedStderr(order), returncode=255, hint=self.HINT)

        self.assertEqual(order, ["reset", "hint"])

    def test_hint_names_the_repo_and_number_it_knows(self):
        self.assertEqual(luv.continue_hint("myrepo", "myrepo-box-42"),
                         "luv continue myrepo 42")
        self.assertEqual(luv.continue_hint("myrepo", "myrepo-42"),
                         "luv continue myrepo 42", "pre-slug folders too")

    def test_hint_degrades_to_what_is_known(self):
        # A brand-new workspace has no number until the remote picks one, and an
        # adopted session may not even have a repo. Both shorter forms still run.
        self.assertEqual(luv.continue_hint("myrepo", "luv-pending-abc123"),
                         "luv continue myrepo")
        self.assertEqual(luv.continue_hint("myrepo", None), "luv continue myrepo")
        self.assertEqual(luv.continue_hint(None, "myrepo-box-42"), "luv continue")

    def test_remote_session_carries_its_own_hint(self):
        with (patch.object(luv.shutil, "which", side_effect=lambda n: f"/bin/{n}"),
              patch.object(luv, "record_session"),
              patch.object(luv, "port_watch", return_value=None),
              patch.object(luv, "hand_over") as hand_over,
              contextlib.redirect_stdout(io.StringIO())):
            luv.dispatch_remote({"host": "box"}, ["myrepo", "42"],
                                workspace="myrepo-box-42",
                                meta={"repo": "myrepo", "org": "acme"})

        self.assertEqual(hand_over.call_args.kwargs.get("hint"),
                         "luv continue myrepo 42")

    def test_a_run_with_no_session_behind_it_gets_no_hint(self):
        # -nit streams to a pipe and exits; there is nothing to continue.
        with (patch.object(luv.shutil, "which", side_effect=lambda n: f"/bin/{n}"),
              patch.object(luv, "port_watch", return_value=None),
              patch.object(luv, "hand_over") as hand_over,
              contextlib.redirect_stdout(io.StringIO())):
            luv.dispatch_remote({"host": "box"}, ["myrepo", "42", "-nit"],
                                use_tmux=False, tty=False)

        self.assertIsNone(hand_over.call_args.kwargs.get("hint"))

    def test_attaching_carries_the_hint_for_the_session_it_picked(self):
        row = {"host": "box", "session": "luv-myrepo-42", "live": True,
               "repo": "myrepo", "workspace": "myrepo-box-42"}
        with (patch.object(luv, "refresh_sessions", return_value=([row], set())),
              patch.object(luv, "resolve_host", return_value={"host": "box"}),
              patch.object(luv, "attach_session") as attach,
              contextlib.redirect_stdout(io.StringIO())):
            luv.cmd_continue(["myrepo", "42"])

        self.assertEqual(attach.call_args.kwargs.get("hint"),
                         "luv continue myrepo 42")

    def test_a_session_that_is_already_gone_points_at_the_reopen(self):
        # Where the hint lands when the agent took the tmux session down with
        # it: the workspace outlives the session, so pass the user on.
        with (patch.object(luv, "refresh_sessions", return_value=([], set())),
              contextlib.redirect_stdout(io.StringIO())):
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    luv.cmd_continue(["myrepo", "42"])

        self.assertIn("luv myrepo 42 -r", err.getvalue())

    def test_nothing_live_at_all_suggests_nothing(self):
        with (patch.object(luv, "refresh_sessions", return_value=([], set())),
              contextlib.redirect_stdout(io.StringIO())):
            with self.assertRaises(SystemExit):
                with contextlib.redirect_stderr(io.StringIO()) as err:
                    luv.cmd_continue([])

        self.assertNotIn("-r", err.getvalue())


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

    @staticmethod
    def _hosts(tmux, origins=None):
        """Stub ssh_run so every host answers for itself.

        Values are that host's `tmux list-sessions` output, or None for a host
        that never answers. Hosts absent from the mapping — the scan always
        covers local plus everything in the config — report nothing running.
        """
        def fake(hc, cmd, **kwargs):
            host = hc["host"] if hc else ""
            if not cmd.startswith("tmux list-sessions"):
                return _completed((origins or {}).get(host, ""))
            answer = tmux.get(host, "")
            if answer is None:
                return _completed("", 255, "ssh: connect: timed out")
            return _completed(answer)
        return patch.object(luv, "ssh_run", side_effect=fake)

    def test_renamed_session_is_matched_by_luv_id(self):
        entry = {"id": "abc123", "host": "box", "session": "luv-pending-abc123",
                 "workspace": None, "repo": "myrepo"}
        line = "abc123|luv-myrepo-42|myrepo-42|1|1700000000\n"

        with self._hosts({"box": line}):
            kept, unreachable = luv.reconcile([entry])

        self.assertEqual(unreachable, set())
        self.assertEqual(len(kept), 1, "the live session must not also be adopted")
        self.assertEqual(kept[0]["session"], "luv-myrepo-42")
        self.assertEqual(kept[0]["workspace"], "myrepo-42")
        self.assertTrue(kept[0]["attached"])
        self.assertTrue(kept[0]["live"])

    def test_dead_session_is_pruned_when_host_answers(self):
        entry = {"id": "abc123", "host": "box", "session": "luv-myrepo-42"}

        with self._hosts({"box": ""}):
            kept, unreachable = luv.reconcile([entry])

        self.assertEqual(kept, [])
        self.assertEqual(unreachable, set())

    def test_unreachable_host_keeps_its_entries(self):
        entry = {"id": "abc123", "host": "box", "session": "luv-myrepo-42"}

        with self._hosts({"box": None}):
            kept, unreachable = luv.reconcile([entry])

        self.assertEqual(len(kept), 1, "an offline host must not wipe the registry")
        self.assertIsNone(kept[0]["live"])
        self.assertEqual(unreachable, {"box"})

    def test_unstamped_session_falls_back_to_name_match(self):
        # A session whose remote luv died before tmux_adopt ran has no @luv_id,
        # which tmux renders as an empty field.
        entry = {"id": "abc123", "host": "box", "session": "luv-myrepo-42"}
        line = "|luv-myrepo-42|myrepo-42|0|1700000000\n"

        with self._hosts({"box": line}):
            kept, _ = luv.reconcile([entry])

        self.assertEqual(len(kept), 1)
        self.assertTrue(kept[0]["live"])

    def test_sessions_without_the_luv_prefix_are_ignored(self):
        entry = {"id": "abc123", "host": "box", "session": "luv-myrepo-42"}
        line = "|someones-other-session||0|1700000000\n"

        with self._hosts({"box": line}):
            kept, _ = luv.reconcile([entry])

        self.assertEqual(kept, [])

    def test_a_session_started_elsewhere_is_adopted(self):
        line = "zz99|luv-myrepo-42|myrepo-42|0|1700000000\n"
        origin = "myrepo-42|git@github.com:acme/myrepo.git\n"

        with self._hosts({"box": line}, origins={"box": origin}):
            kept, _ = luv.reconcile([])

        self.assertEqual(len(kept), 1, "an empty registry must not mean empty output")
        self.assertEqual(kept[0], {
            "id": "zz99", "host": "box", "session": "luv-myrepo-42",
            "workspace": "myrepo-42", "org": "acme", "repo": "myrepo",
            "adopted": True, "last_seen": kept[0]["last_seen"],
            "attached": False, "activity": 1700000000, "live": True,
        })

    def test_a_configured_host_is_scanned_without_any_entry_for_it(self):
        # 'gpu' only exists under remote.hosts — nothing on this machine has
        # ever dispatched to it, so the registry offers no reason to look.
        line = "|luv-myrepo-gpu-7|myrepo-gpu-7|1|1700000000\n"

        with self._hosts({"gpu": line}):
            kept, _ = luv.reconcile([])

        self.assertEqual([(s["host"], s["session"]) for s in kept],
                         [("gpu", "luv-myrepo-gpu-7")])
        self.assertTrue(kept[0]["id"], "an unstamped session still needs an id")

    def test_a_slugged_workspace_takes_its_repo_from_git_not_the_name(self):
        # 'myrepo-mbp-42' cannot be split into repo and slug by looking at it —
        # the origin on the host is the only thing that actually knows.
        line = "zz99|luv-myrepo-mbp-42|myrepo-mbp-42|0|1700000000\n"
        origin = "myrepo-mbp-42|git@github.com:acme/myrepo.git\n"

        with self._hosts({"box": line}, origins={"box": origin}):
            kept, _ = luv.reconcile([])

        self.assertEqual((kept[0]["org"], kept[0]["repo"]), ("acme", "myrepo"))

    def test_an_adopted_session_is_matched_not_adopted_twice(self):
        line = "zz99|luv-myrepo-42|myrepo-42|0|1700000000\n"

        with self._hosts({"box": line}):
            first, _ = luv.reconcile([])
            luv.save_sessions(first)
            again, _ = luv.reconcile(luv.load_sessions())

        self.assertEqual(len(again), 1)
        self.assertEqual(again[0]["id"], "zz99")

    def test_an_owner_is_backfilled_once_the_workspace_has_a_name(self):
        # Adopted mid-dispatch, while the session was still luv-pending-zz99 and
        # the clone had no folder to read an origin from.
        entry = {"id": "zz99", "host": "box", "session": "luv-pending-zz99",
                 "workspace": None, "adopted": True}
        line = "zz99|luv-myrepo-42|myrepo-42|0|1700000000\n"
        origin = "myrepo-42|https://github.com/acme/myrepo\n"

        with self._hosts({"box": line}, origins={"box": origin}):
            kept, _ = luv.reconcile([entry])

        self.assertEqual(len(kept), 1)
        self.assertEqual((kept[0]["org"], kept[0]["repo"]), ("acme", "myrepo"))

    def test_a_local_session_is_listed_too(self):
        line = "|my-own-tmux|myrepo-3|1|1700000000\n"

        with self._hosts({"": line}):
            kept, unreachable = luv.reconcile([])

        self.assertEqual(kept[0]["host"], "")
        self.assertEqual(kept[0]["workspace"], "myrepo-3")
        self.assertEqual(unreachable, set(), "local is never unreachable")

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


def _shell_env_output(env, before="", after=""):
    """What the probe sees: an rc's greeting, the marker, the environment."""
    return f"{before}__luv_env__{json.dumps(env)}{after}"


class ShellEnvTests(unittest.TestCase):
    """tmux and ssh exec without a shell, so the rc has to be asked for."""

    def _probe(self, stdout, returncode=0):
        with (patch.dict(luv.os.environ, {"SHELL": "/bin/zsh"}, clear=True),
              patch.object(luv, "run",
                           return_value=_completed(stdout, returncode)) as run):
            return luv.shell_env(), run.call_args

    def test_asks_the_users_own_shell_as_login_and_interactive(self):
        env, call = self._probe(_shell_env_output({"API_BASE": "https://x"}))

        self.assertEqual(env, {"API_BASE": "https://x"})
        self.assertEqual(call.args[0][:2], ["/bin/zsh", "-lic"])
        self.assertEqual(call.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(call.kwargs["timeout"], luv.SHELL_ENV_TIMEOUT)

    def test_greeting_before_the_marker_is_ignored(self):
        env, _ = self._probe(_shell_env_output({"A": "1"}, before="welcome back!\n"))

        self.assertEqual(env, {"A": "1"})

    def test_exit_hook_after_the_environment_is_ignored(self):
        env, _ = self._probe(_shell_env_output({"A": "1"}, after="\ngoodbye\n"))

        self.assertEqual(env, {"A": "1"})

    def test_failing_rc_still_yields_what_it_exported(self):
        env, _ = self._probe(_shell_env_output({"A": "1"}), returncode=1)

        self.assertEqual(env, {"A": "1"})

    def test_a_shell_that_says_nothing_useful_is_not_an_error(self):
        self.assertEqual(self._probe("")[0], {})
        self.assertEqual(self._probe("__luv_env__not json")[0], {})

    def test_timeout_is_not_an_error(self):
        # run() turns a hung rc into returncode 124 rather than an exception.
        self.assertEqual(self._probe("", returncode=124)[0], {})


class ApplyShellEnvTests(unittest.TestCase):
    def _apply(self, environ, rc, config=None):
        with (patch.dict(luv.os.environ, environ, clear=True),
              patch.object(luv, "load_config", return_value=config or {}),
              patch.object(luv, "shell_env", return_value=rc) as probe):
            luv.apply_shell_env()
            return dict(luv.os.environ), probe.called

    def test_only_runs_for_the_luv_tmux_or_ssh_started(self):
        env, probed = self._apply({}, {"API_KEY": "from-rc"})

        self.assertFalse(probed)
        self.assertNotIn("API_KEY", env)

    def test_fills_in_what_the_session_never_sourced(self):
        env, _ = self._apply({"_LUV_INNER": "1"}, {"API_KEY": "from-rc"})

        self.assertEqual(env["API_KEY"], "from-rc")

    def test_what_the_caller_set_wins(self):
        env, _ = self._apply({"_LUV_INNER": "1", "API_KEY": "explicit"},
                             {"API_KEY": "from-rc", "OTHER": "from-rc"})

        self.assertEqual(env["API_KEY"], "explicit")
        self.assertEqual(env["OTHER"], "from-rc")

    def test_path_is_merged_rather_than_replaced(self):
        env, _ = self._apply({"_LUV_INNER": "1", "PATH": "/usr/bin:/bin"},
                             {"PATH": "/home/u/.nvm/bin:/usr/bin:/home/u/.local/bin"})

        self.assertEqual(env["PATH"],
                         "/usr/bin:/bin:/home/u/.nvm/bin:/home/u/.local/bin")

    def test_the_probe_shells_own_bookkeeping_is_left_behind(self):
        env, _ = self._apply({"_LUV_INNER": "1"},
                             {"PWD": "/home/u", "SHLVL": "3", "_": "/usr/bin/env",
                              "OLDPWD": "/tmp", "KEEP": "yes"})

        self.assertEqual(env["KEEP"], "yes")
        for key in ("PWD", "SHLVL", "_", "OLDPWD"):
            self.assertNotIn(key, env)

    def test_config_can_turn_it_off(self):
        env, probed = self._apply({"_LUV_INNER": "1"}, {"API_KEY": "from-rc"},
                                  config={"shell_env": False})

        self.assertFalse(probed)
        self.assertNotIn("API_KEY", env)


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

    def test_branch_comes_from_git_not_the_folder_name(self):
        # A handed-over folder carries another machine's slug, so rebuilding the
        # branch from the name would fetch the wrong ref — or none at all.
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        (root / "myrepo-box-42").mkdir()
        seen = {}

        def fake_run(cmd, cwd=None):
            if cmd[:2] == ["git", "rev-parse"] and "--abbrev-ref" in cmd:
                return _completed("luv-mbp-42\n")
            if cmd[:2] == ["git", "fetch"]:
                seen["fetch"] = cmd
                return _completed(returncode=1)
            return _completed()

        with (patch.object(luv, "PRS_DIR", root),
              patch.object(luv, "live_tmux_sessions", return_value=set()),
              patch.object(luv, "run", side_effect=fake_run),
              patch.object(luv, "parse_github_remote", return_value=None),
              patch.object(luv, "_force_rmtree"),
              contextlib.redirect_stdout(io.StringIO())):
            luv.cmd_clean(force=False)

        self.assertEqual(seen["fetch"], ["git", "fetch", "origin", "luv-mbp-42"])


class NamingTests(unittest.TestCase):
    """Workspace and branch names have to be unique per machine."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        p = patch.object(luv, "PRS_DIR", self.root)
        p.start()
        self.addCleanup(p.stop)

    def _slug(self, slug):
        return patch.object(luv, "machine_slug", return_value=slug)

    def test_configured_machine_name_wins(self):
        with patch.object(luv, "load_config", return_value={"machine": "MBP-16!"}):
            self.assertEqual(luv.machine_slug(), "mbp16")

    def test_hostname_is_the_fallback(self):
        with (patch.object(luv, "load_config", return_value={}),
              patch.object(luv.socket, "gethostname",
                           return_value="Niveds-MacBook.local")):
            self.assertEqual(luv.machine_slug(), "nivedsma")

    def test_slug_never_contains_a_separator(self):
        # workspace_re reads '{repo}-{slug}-{number}'; a '-' inside the slug
        # would make that ambiguous.
        self.assertNotIn("-", luv.sanitize_slug("gpu-box-01"))
        self.assertEqual(luv.sanitize_slug("gpu-box-01"), "gpubox01")

    def test_unusable_machine_name_falls_back(self):
        with (patch.object(luv, "load_config", return_value={"machine": "!!!"}),
              patch.object(luv.socket, "gethostname", return_value="???")):
            self.assertEqual(luv.machine_slug(), "local")

    def test_names_carry_the_slug(self):
        with self._slug("mbp"):
            self.assertEqual(luv.workspace_name("myrepo", 42), "myrepo-mbp-42")
            self.assertEqual(luv.branch_name(42), "luv-mbp-42")
            self.assertEqual(luv.tmux_session_name(luv.workspace_name("myrepo", 42)),
                             "luv-myrepo-mbp-42")
            self.assertEqual(luv.docker_project_name(self.root / "myrepo-mbp-42"),
                             "luv-myrepo-mbp-42")

    def test_number_is_read_from_either_form(self):
        self.assertEqual(luv.workspace_number("myrepo", "myrepo-mbp-42"), 42)
        self.assertEqual(luv.workspace_number("myrepo", "myrepo-42"), 42)
        self.assertIsNone(luv.workspace_number("myrepo", "other-mbp-42"))

    def test_repeat_clones_of_a_pr_get_their_own_folder(self):
        with self._slug("mbp"):
            self.assertEqual(luv.next_workspace_dir("myrepo", 42).name,
                             "myrepo-mbp-42")
            (self.root / "myrepo-mbp-42").mkdir()
            self.assertEqual(luv.next_workspace_dir("myrepo", 42).name,
                             "myrepo-mbp-42_2")
            (self.root / "myrepo-mbp-42_2").mkdir()
            self.assertEqual(luv.next_workspace_dir("myrepo", 42).name,
                             "myrepo-mbp-42_3")

    def test_a_copy_is_still_workspace_42(self):
        self.assertEqual(luv.workspace_number("myrepo", "myrepo-mbp-42_2"), 42)
        self.assertEqual(luv.folder_number("myrepo-mbp-42_2"), 42)
        self.assertEqual(luv.folder_number("myrepo-42_11"), 42)
        self.assertIsNone(luv.folder_number("myrepo-main_2"))

    def test_copy_suffix_survives_every_derived_name(self):
        # tmux rejects '.' and ':' in session names and Compose rejects '.' in
        # project names, which is why the separator is '_'.
        name = luv.workspace_name("myrepo", 42, slug="mbp", copy=2)
        self.assertEqual(name, "myrepo-mbp-42_2")
        self.assertEqual(luv.tmux_session_name(name), "luv-myrepo-mbp-42_2")
        self.assertEqual(luv.docker_project_name(self.root / name),
                         "luv-myrepo-mbp-42_2")

    def test_find_workspace_opens_the_newest_copy(self):
        for name in ("myrepo-mbp-42", "myrepo-mbp-42_2", "myrepo-mbp-42_10"):
            (self.root / name).mkdir()
        with self._slug("mbp"):
            self.assertEqual(luv.find_workspace("myrepo", 42).name,
                             "myrepo-mbp-42_10")

    def test_copies_of_one_machines_folder_are_not_ambiguous(self):
        # Two copies made *here* are a -l history, not a naming conflict; only
        # two different machines leave the number genuinely undecidable.
        for name in ("myrepo-box-42", "myrepo-box-42_2"):
            (self.root / name).mkdir()
        with self._slug("mbp"):
            self.assertEqual(luv.find_workspace("myrepo", 42).name,
                             "myrepo-box-42_2")

    def test_find_latest_clone_prefers_the_newest_copy(self):
        for name in ("myrepo-mbp-41", "myrepo-mbp-41_2"):
            (self.root / name).mkdir()
        with self._slug("mbp"):
            self.assertEqual(luv.find_latest_clone("myrepo").name,
                             "myrepo-mbp-41_2")

    def test_find_workspace_prefers_our_own(self):
        for name in ("myrepo-42", "myrepo-box-42", "myrepo-mbp-42"):
            (self.root / name).mkdir()
        with self._slug("mbp"):
            self.assertEqual(luv.find_workspace("myrepo", 42).name, "myrepo-mbp-42")

    def test_find_workspace_finds_a_handed_over_folder(self):
        # It keeps the slug of the machine that made it, which is not this one.
        (self.root / "myrepo-box-42").mkdir()
        with self._slug("mbp"):
            self.assertEqual(luv.find_workspace("myrepo", 42).name, "myrepo-box-42")

    def test_find_workspace_still_finds_pre_slug_folders(self):
        (self.root / "myrepo-42").mkdir()
        with self._slug("mbp"):
            self.assertEqual(luv.find_workspace("myrepo", 42).name, "myrepo-42")

    def test_two_foreign_candidates_are_ambiguous(self):
        for name in ("myrepo-box-42", "myrepo-gpu-42"):
            (self.root / name).mkdir()
        with self._slug("mbp"), self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            luv.find_workspace("myrepo", 42)

    def test_find_latest_clone_spans_both_forms(self):
        for name in ("myrepo-7", "myrepo-box-41", "myrepo-mbp-9", "other-mbp-99"):
            (self.root / name).mkdir()
        with self._slug("mbp"):
            self.assertEqual(luv.find_latest_clone("myrepo").name, "myrepo-box-41")

    def test_branch_re_matches_both_forms(self):
        self.assertTrue(luv.branch_re(42).match("luv-mbp-42"))
        self.assertTrue(luv.branch_re(42).match("luv-42"))
        self.assertFalse(luv.branch_re(42).match("luv-mbp-420"))

    def test_where_reports_the_folder_a_host_would_use(self):
        (self.root / "myrepo-box-42").mkdir()
        out = io.StringIO()
        with self._slug("mbp"), contextlib.redirect_stdout(out):
            luv.cmd_where(["exo/myrepo", "42"])
            luv.cmd_where(["exo/myrepo", "77"])

        self.assertEqual(out.getvalue().split(), ["myrepo-box-42", "myrepo-mbp-77"])


class OpenPrTests(unittest.TestCase):
    """`luv -l` clones the PR again rather than reopening what is lying around."""

    SAME_REPO = json.dumps({
        "head": {"ref": "feature-branch",
                 "repo": {"clone_url": "https://github.com/exo/myrepo.git"}},
        "base": {"repo": {"clone_url": "https://github.com/exo/myrepo.git"}},
    })
    FROM_FORK = json.dumps({
        "head": {"ref": "feature-branch",
                 "repo": {"clone_url": "https://github.com/someone/myrepo.git"}},
        "base": {"repo": {"clone_url": "https://github.com/exo/myrepo.git"}},
    })

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.git = []
        for p in (patch.object(luv, "PRS_DIR", self.root),
                  patch.object(luv, "machine_slug", return_value="mbp"),
                  patch.object(luv, "ensure_pr_rules")):
            p.start()
            self.addCleanup(p.stop)

    def _open(self, pr_json=SAME_REPO, **kwargs):
        """Run open_pr with git stubbed out; returns the launched folder."""
        with (patch.object(luv, "run", return_value=_completed(pr_json)),
              patch.object(luv.subprocess, "run",
                           side_effect=lambda cmd, **kw: self.git.append(cmd)
                           or _completed()),
              patch.object(luv, "launch") as launch,
              patch.object(luv, "navigate") as navigate,
              patch.object(luv, "resume") as resume,
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(io.StringIO())):
            luv.open_pr("exo", "myrepo", 42, None, **kwargs)
        called = next(m for m in (launch, navigate, resume) if m.called)
        return called.call_args.args[0]

    def test_a_fresh_clone_lands_next_to_the_old_folder(self):
        (self.root / "myrepo-mbp-42").mkdir()

        folder = self._open(fresh=True)

        self.assertEqual(folder.name, "myrepo-mbp-42_2")
        self.assertIn(["git", "checkout", "feature-branch"], self.git)

    def test_without_fresh_the_existing_folder_is_reused(self):
        # -pr keeps the old behaviour: it names a workspace, not a URL.
        (self.root / "myrepo-mbp-42").mkdir()

        self.assertEqual(self._open().name, "myrepo-mbp-42")
        self.assertEqual(self.git, [], "reopening must not clone")

    def test_resume_reopens_rather_than_recloning(self):
        # A fresh clone has no conversation in it, so there is nothing to resume.
        (self.root / "myrepo-mbp-42").mkdir()

        self.assertEqual(self._open(fresh=True, resume_mode=True).name,
                         "myrepo-mbp-42")
        self.assertEqual(self.git, [])

    def test_a_fork_pr_also_fetches_the_base_repo(self):
        self._open(self.FROM_FORK, fresh=True)

        self.assertIn(["git", "remote", "add", "upstream",
                       "https://github.com/exo/myrepo.git"], self.git)
        self.assertIn(["git", "fetch", "upstream"], self.git)

    def test_a_same_repo_pr_needs_no_second_remote(self):
        # The clone already carries every branch; a second remote for the same
        # URL would just duplicate them.
        self._open(fresh=True)

        self.assertNotIn("upstream", [a for cmd in self.git for a in cmd])

    def test_a_deleted_fork_fails_before_touching_the_disk(self):
        gone = json.dumps({"head": {"ref": "feature-branch", "repo": None},
                           "base": {"repo": {"clone_url": "https://x/y.git"}}})

        with self.assertRaises(SystemExit), \
                contextlib.redirect_stderr(io.StringIO()):
            self._open(gone, fresh=True)
        self.assertEqual(self.git, [])


class HandoverTests(unittest.TestCase):
    """Moving a workspace between machines."""

    SRC = Path("/home/u/prs")
    DST = Path("/remote/prs")

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        (root / "prs").mkdir()
        self.prs = root / "prs"
        self.patches = [
            patch.object(luv, "LUV_DIR", root),
            patch.object(luv, "SESSIONS_FILE", root / "sessions.json"),
            patch.object(luv, "SESSIONS_LOCK", root / "sessions.lock"),
            patch.object(luv, "PRS_DIR", self.prs),
            patch.object(luv, "machine_slug", return_value="mbp"),
            patch.object(luv, "load_config", return_value=REMOTE_CONFIG),
        ]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    def _run(self, args, ssh=None, sessions=(), **kwargs):
        """Hand over with the network stubbed; returns (ssh calls, mocks).

        The call list is also kept on self, so a test that expects cmd_handover
        to bail out can still see how far it got.
        """
        calls = self._calls = []

        def fake_ssh(hc, cmd, **kw):
            calls.append((luv.host_label(hc), cmd))
            if ssh:
                override = ssh(cmd)
                if override is not None:
                    return override
            # No folder in the way on the destination; a session running on the
            # source, so no confirmation is needed.
            return _completed(returncode=1) if "test -e" in cmd else _completed()

        def fake_paths(hc):
            return (Path("/home/u"), self.prs) if hc is None else \
                   (Path("/remote"), self.DST)

        with (patch.object(luv, "ssh_run", side_effect=fake_ssh),
              patch.object(luv, "preflight_host"),
              patch.object(luv, "refresh_sessions",
                           return_value=(list(sessions), set())),
              patch.object(luv, "remote_paths", side_effect=fake_paths),
              patch.object(luv, "parse_github_remote", return_value=("exo", "myrepo")),
              patch.object(luv, "stream_copy") as stream,
              patch.object(luv, "dispatch_remote") as dispatch,
              patch.object(luv, "start_local_session") as local,
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(io.StringIO())):
            luv.cmd_handover(args, **kwargs)
        return calls, {"stream": stream, "dispatch": dispatch, "local": local}

    def _workspace(self, name="myrepo-mbp-42"):
        (self.prs / name).mkdir()

    def test_destination_is_required(self):
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self._run(["myrepo", "42"])

    def test_same_machine_is_rejected(self):
        self._workspace()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self._run(["myrepo", "42"], to="local")

    def test_local_workspace_is_found_without_a_registry_entry(self):
        # A session started on this machine was never recorded, and that is the
        # usual source of a handover.
        self._workspace()
        _, mocks = self._run(["myrepo", "42"], to="box")

        args, kwargs = mocks["dispatch"].call_args
        self.assertEqual(args[1], ["exo/myrepo", "42", "-r"])
        self.assertEqual(kwargs["workspace"], "myrepo-mbp-42")

    def test_codex_and_model_are_replayed(self):
        self._workspace()
        entry = {"id": "abc", "host": "box", "session": "luv-myrepo-gpu-9",
                 "org": "exo", "repo": "myrepo", "workspace": "myrepo-gpu-9",
                 "agent": "codex", "model": "gpt-5", "prompt": "keep going",
                 "live": True}
        _, mocks = self._run(["myrepo", "9"], to="local", sessions=[entry])

        args = mocks["local"].call_args.args
        self.assertEqual(args[0], "myrepo-gpu-9")  # slug is sticky
        self.assertEqual(args[1], ["exo/myrepo", "9", "-r", "--codex", "-m", "gpt-5"])
        self.assertEqual(args[2]["prompt"], "keep going")

    def test_existing_destination_folder_aborts_before_anything_is_killed(self):
        self._workspace()
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self._run(["myrepo", "42"], to="box",
                      ssh=lambda cmd: _completed())  # every path already exists

        self.assertNotIn("kill-session",
                         " ".join(cmd for _, cmd in self._calls))

    def test_source_is_stopped_and_docker_torn_down(self):
        self._workspace()
        calls, _ = self._run(["myrepo", "42"], to="box")
        joined = " ".join(cmd for _, cmd in calls)

        self.assertIn("tmux kill-session -t luv-myrepo-mbp-42", joined)
        self.assertIn("docker compose -p luv-myrepo-mbp-42 down", joined)

    def test_source_folder_is_kept_by_default(self):
        self._workspace()
        calls, _ = self._run(["myrepo", "42"], to="box")

        self.assertNotIn("rm -rf", " ".join(cmd for _, cmd in calls))

    def test_purge_removes_the_source(self):
        self._workspace()
        calls, _ = self._run(["myrepo", "42"], to="box", purge=True)

        self.assertIn(f"rm -rf {self.prs}/myrepo-mbp-42",
                      " ".join(cmd for _, cmd in calls))

    def test_agent_state_is_a_second_stream(self):
        self._workspace()
        _, mocks = self._run(["myrepo", "42"], to="box")

        self.assertEqual(mocks["stream"].call_count, 2)

    def test_no_agent_state_copies_only_the_workspace(self):
        self._workspace()
        _, mocks = self._run(["myrepo", "42"], to="box", no_agent_state=True)

        self.assertEqual(mocks["stream"].call_count, 1)

    def test_a_mismatched_copy_stops_before_restarting(self):
        self._workspace()

        def ssh(cmd):
            if "rev-parse HEAD" in cmd:
                # Different answers for source and destination.
                ssh.n += 1
                return _completed(f"sha{ssh.n} 0\n")
            return None
        ssh.n = 0

        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            self._run(["myrepo", "42"], to="box", ssh=ssh)


class TransferTests(unittest.TestCase):
    """The tar relay and the transcript rewrite."""

    def test_local_endpoints_skip_ssh(self):
        argv = luv.tar_send_argv(None, Path("/a"), ["ws"])

        self.assertEqual(argv, ["tar", "-C", "/a", "-czf", "-", "ws"])

    def test_remote_send_never_allocates_a_tty(self):
        # ssh -t would translate newlines and corrupt the tar stream.
        argv = luv.tar_send_argv({"host": "box"}, Path("/a"), ["ws"])

        self.assertEqual(argv[0], "ssh")
        self.assertNotIn("-t", argv)
        self.assertIn("tar -C /a -czf - ws", argv[-1])

    def test_remote_receive_creates_the_root_first(self):
        argv = luv.tar_recv_argv({"host": "box"}, Path("/b"))

        self.assertIn("mkdir -p /b && tar -C /b -xzf -", argv[-1])

    def test_claude_project_slug_matches_claudes_own(self):
        self.assertEqual(luv.claude_project_slug(Path("/Users/n/prs/myrepo-mbp-43")),
                         "-Users-n-prs-myrepo-mbp-43")

    def test_rewrite_is_a_noop_when_paths_agree(self):
        self.assertEqual(luv.rewrite_script("f", "/same", "/same"), "true")

    def test_rewrite_avoids_sed_dash_i(self):
        # -i takes an argument on BSD sed and not on GNU sed; the laptop half of
        # a handover is usually a Mac.
        script = luv.rewrite_script("'/d'/*.jsonl", "/old/ws", "/new/ws")

        self.assertNotIn("sed -i", script)
        self.assertIn("s|/old/ws|/new/ws|g", script)

    def test_stream_copy_does_nothing_without_members(self):
        with patch.object(luv.subprocess, "Popen") as popen:
            luv.stream_copy(None, Path("/a"), [], None, Path("/b"))

        self.assertFalse(popen.called)


class FolderNumberTests(unittest.TestCase):
    """The repo-agnostic parse, used where only a directory listing is in hand."""

    def test_parses_trailing_number(self):
        self.assertEqual(luv.folder_number("myrepo-42"), 42)
        self.assertEqual(luv.folder_number("myrepo-mbp-42"), 42)

    def test_handles_repos_with_hyphens(self):
        self.assertEqual(luv.folder_number("my-cool-repo-7"), 7)

    def test_rejects_non_workspace_names(self):
        self.assertIsNone(luv.folder_number("myrepo"))
        self.assertIsNone(luv.folder_number("myrepo-main"))
        self.assertIsNone(luv.folder_number(None))


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

    def test_a_slugged_workspace_asks_for_its_own_branch(self):
        # The folder keeps the slug of the machine that made it, and so does the
        # branch — asking for luv-42 here would find nothing.
        rows = [self._session(workspace="myrepo-box-42")]

        with patch.object(luv, "run", return_value=_completed(self._PR_JSON)) as run:
            luv.attach_pr_links(rows)

        argv = run.call_args.args[0]
        self.assertEqual(argv[argv.index("--head") + 1], "luv-box-42")

    def test_a_folder_from_another_repo_is_skipped(self):
        rows = [self._session(workspace="otherrepo-box-42")]

        with patch.object(luv, "run") as run:
            luv.attach_pr_links(rows)

        self.assertFalse(run.called, "the folder is not this repo's workspace")

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



# Trimmed from a real host running two luv workspaces. Every shape that matters
# is here: a published port, its IPv6 twin, and a merely-exposed one.
DOCKER_PS = (
    "agenteye-legion-511_2|dashboard|0.0.0.0:3000->3000/tcp, [::]:3000->3000/tcp\n"
    "agenteye-legion-524|clickhouse|9009/tcp, 0.0.0.0:8223->8123/tcp, "
    "[::]:8223->8123/tcp, 0.0.0.0:9900->9000/tcp, [::]:9900->9000/tcp\n"
    "unrelated-project|web|0.0.0.0:4000->4000/tcp\n"
)

# ss fills in users:(...) only for our own processes; the blank rows are root's
# and other people's, and dropping them is the whole point.
SS_OUTPUT = (
    'LISTEN 0 128   127.0.0.1:36891 0.0.0.0:* '
    'users:(("code-1b6a188127",pid=1175442,fd=12))\n'
    'LISTEN 0 4096    0.0.0.0:8223  0.0.0.0:*\n'
    'LISTEN 0 511       [::]:5173     [::]:* users:(("node",pid=48213,fd=21))\n'
)


class PortProbeTests(unittest.TestCase):
    """What a host reports it is listening on, and who it belongs to."""

    def test_only_published_docker_ports_are_offered(self):
        with patch.object(luv.shutil, "which", return_value="/bin/docker"), \
             patch.object(luv, "run", return_value=_completed(DOCKER_PS)):
            found = luv.docker_listeners({"agenteye-legion-524"})

        # 9009/tcp is exposed to the compose network with nothing on the host to
        # point a tunnel at, and the [::] rows are the same mapping listed twice.
        self.assertEqual(found, {8223: ("agenteye-legion-524", "clickhouse"),
                                 9900: ("agenteye-legion-524", "clickhouse")})

    def test_compose_project_named_after_the_folder_is_ours(self):
        """An agent running `docker compose up` itself gets the directory name."""
        with patch.object(luv.shutil, "which", return_value="/bin/docker"), \
             patch.object(luv, "run", return_value=_completed(DOCKER_PS)):
            found = luv.docker_listeners({"agenteye-legion-511_2"})

        self.assertEqual(found, {3000: ("agenteye-legion-511_2", "dashboard")})

    def test_compose_project_named_by_luv_is_ours_too(self):
        ps = "luv-myrepo-box-42|web|0.0.0.0:8080->80/tcp\n"
        with patch.object(luv.shutil, "which", return_value="/bin/docker"), \
             patch.object(luv, "run", return_value=_completed(ps)):
            found = luv.docker_listeners({"myrepo-box-42"})

        self.assertEqual(found, {8080: ("myrepo-box-42", "web")})

    def test_another_projects_containers_are_left_alone(self):
        with patch.object(luv.shutil, "which", return_value="/bin/docker"), \
             patch.object(luv, "run", return_value=_completed(DOCKER_PS)):
            found = luv.docker_listeners({"agenteye-legion-524"})

        self.assertNotIn(4000, found)

    def test_ss_reports_only_processes_we_own(self):
        self.assertEqual(luv.parse_ss(SS_OUTPUT),
                         [(36891, 1175442, "code-1b6a188127"),
                          (5173, 48213, "node")])

    def test_lsof_fallback_groups_files_under_their_process(self):
        out = "p48213\ncnode\nn127.0.0.1:5173\nn*:5174\np99\ncpostgres\nn*:5432\n"

        self.assertEqual(luv.parse_lsof(out),
                         [(5173, 48213, "node"), (5174, 48213, "node"),
                          (5432, 99, "postgres")])

    def test_ancestry_walk_reaches_the_pane_through_the_shell(self):
        # node <- npm <- bash <- claude <- the pane itself
        tree = {48213: (48200, "node"), 48200: (47990, "npm"),
                47990: (47980, "bash"), 47980: (47001, "claude")}
        roots = {47001: ("abc123", "myrepo-box-42", "luv-myrepo-box-42")}

        self.assertEqual(luv.owning_pane(48213, tree, roots),
                         ("abc123", "myrepo-box-42", "luv-myrepo-box-42"))

    def test_a_listener_outside_every_pane_belongs_to_nobody(self):
        tree = {900: (1, "sshd")}
        roots = {47001: ("abc123", "myrepo-box-42", "luv-myrepo-box-42")}

        self.assertIsNone(luv.owning_pane(900, tree, roots))

    def test_a_pid_cycle_cannot_hang_the_walk(self):
        tree = {10: (11, "a"), 11: (10, "b")}

        self.assertIsNone(luv.owning_pane(10, tree, {}))

    def _listening(self, panes, ss="", docker=""):
        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["tmux", "list-panes"]:
                return _completed(panes)
            if cmd[0] == "ss":
                return _completed(ss)
            if cmd[0] == "ps":
                return _completed("48213 47001 node\n47001 1 claude\n")
            if cmd[0] == "docker":
                return _completed(docker)
            return _completed("", 1)
        out = io.StringIO()
        with patch.object(luv, "run", side_effect=fake_run), \
             patch.object(luv.shutil, "which", return_value="/bin/docker"), \
             patch.object(luv, "load_config", return_value={}), \
             contextlib.redirect_stdout(out):
            luv.cmd_listening()
        return out.getvalue().splitlines()

    def test_docker_wins_over_the_process_walk_for_the_same_port(self):
        """ss sees a published port as docker-proxy; the service name is better."""
        lines = self._listening(
            panes="abc|myrepo-box-42|luv-myrepo-box-42|47001\n",
            ss='LISTEN 0 4096 0.0.0.0:3000 0.0.0.0:* users:(("node",pid=48213,fd=9))\n',
            docker="myrepo-box-42|dashboard|0.0.0.0:3000->3000/tcp\n")

        self.assertEqual(lines, ["abc|myrepo-box-42|luv-myrepo-box-42|3000|dashboard"])

    def test_a_pane_that_is_not_luvs_is_ignored(self):
        lines = self._listening(
            panes="||my-own-tmux|47001\n",
            ss='LISTEN 0 4096 0.0.0.0:3000 0.0.0.0:* users:(("node",pid=48213,fd=9))\n')

        self.assertEqual(lines, [])

    def test_privileged_ports_are_left_out(self):
        lines = self._listening(
            panes="abc|myrepo-box-42|luv-myrepo-box-42|47001\n",
            ss='LISTEN 0 4096 0.0.0.0:80 0.0.0.0:* users:(("node",pid=48213,fd=9))\n')

        self.assertEqual(lines, [])


class PortForwardTests(unittest.TestCase):
    """Building tunnels, choosing local ports, and keeping them in step."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.patches = [
            patch.object(luv, "LUV_DIR", root),
            patch.object(luv, "TUNNEL_DIR", root / "tun"),
            patch.object(luv, "SESSIONS_FILE", root / "sessions.json"),
            patch.object(luv, "SESSIONS_LOCK", root / "sessions.lock"),
            patch.object(luv, "load_config", return_value=REMOTE_CONFIG),
        ]
        for p in self.patches:
            p.start()
        luv._forward_warned.clear()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tempdir.cleanup()

    HC = {"host": "box", "identity_file": "/keys/box_key"}

    def test_control_command_comes_before_the_host(self):
        """ssh reads anything after the host as the command to run there."""
        argv = luv.ssh_base(self.HC, batch=True, control="/tmp/s.sock",
                            control_op=["-O", "forward", "-L", "1:2:3:4"])

        self.assertEqual(argv[-1], "box")
        self.assertLess(argv.index("-O"), argv.index("box"))
        self.assertEqual(argv[argv.index("-O") + 1], "forward")

    def test_master_is_told_to_outlive_this_process(self):
        argv = luv.ssh_base(self.HC, batch=True, control="/tmp/s.sock", master=True)

        self.assertIn("-M", argv)
        self.assertIn("-N", argv)
        self.assertIn("-f", argv)
        self.assertIn("ControlPersist=yes", argv)

    def test_an_ordinary_call_is_untouched_by_any_of_this(self):
        """The interactive session must not share the forwarder's connection."""
        self.assertEqual(luv.ssh_base(self.HC, tty=True),
                         ["ssh", "-t", "-i", "/keys/box_key", "box"])

    def test_socket_path_does_not_grow_with_the_host_name(self):
        long_host = {"host": "a-very-long-hostname." * 6}

        # ControlPath has to fit a sockaddr_un: 104 bytes on macOS.
        self.assertEqual(len(luv.tunnel_socket(long_host).name),
                         len(luv.tunnel_socket(self.HC).name))
        self.assertLess(len(luv.tunnel_socket(long_host).name), 24)

    def test_differently_keyed_hosts_get_different_sockets(self):
        self.assertNotEqual(luv.tunnel_socket(self.HC),
                            luv.tunnel_socket({"host": "box", "port": 2222}))

    def test_both_ends_of_a_forward_are_loopback(self):
        cfg = luv.ports_config()

        # 0.0.0.0 on the near end would republish someone's dev server to the
        # LAN; 127.0.0.1 on the far end is right either way the server bound.
        self.assertEqual(luv.forward_spec(cfg, 3001, 3000),
                         "127.0.0.1:3001:127.0.0.1:3000")

    def test_unreachable_host_is_distinguished_from_a_quiet_one(self):
        with patch.object(luv, "ssh_run",
                          return_value=_completed("", 255, "ssh: timed out")):
            self.assertIsNone(luv.query_ports(self.HC))

        with patch.object(luv, "ssh_run", return_value=_completed("")):
            self.assertEqual(luv.query_ports(self.HC), [])

    def test_a_host_running_an_older_luv_reports_no_ports(self):
        stale = _completed("", 1, "luv: error: repo not found")

        with patch.object(luv, "ssh_run", return_value=stale):
            self.assertEqual(luv.query_ports(self.HC), [])

    def test_local_port_mirrors_the_remote_one_when_it_is_free(self):
        with patch.object(luv, "port_free", return_value=True):
            self.assertEqual(luv.pick_local_port(3000, set(), "127.0.0.1"), 3000)

    def test_local_port_walks_up_past_a_collision(self):
        with patch.object(luv, "port_free", side_effect=lambda p, b: p != 3000):
            self.assertEqual(luv.pick_local_port(3000, set(), "127.0.0.1"), 3001)

        with patch.object(luv, "port_free", return_value=True):
            self.assertEqual(luv.pick_local_port(3000, {3000}, "127.0.0.1"), 3001)

    def test_a_mapping_already_in_use_is_kept(self):
        """A URL that worked a minute ago should still work."""
        entry = {"workspace": "w",
                 "forwards": [{"remote": 3000, "local": 3007, "label": "web"}]}
        rows = [{"id": "", "workspace": "w", "session": "s", "port": 3000,
                 "label": "web"}]

        with patch.object(luv, "port_free", return_value=True):
            want = luv.desired_forwards(entry, rows, luv.ports_config(), set(),
                                        {3007: 3000})

        self.assertEqual(want, [{"remote": 3000, "local": 3007, "label": "web"}])

    def test_a_mapping_someone_else_took_is_reallocated(self):
        entry = {"workspace": "w",
                 "forwards": [{"remote": 3000, "local": 3007, "label": "web"}]}
        rows = [{"id": "", "workspace": "w", "session": "s", "port": 3000,
                 "label": "web"}]

        with patch.object(luv, "port_free", return_value=True):
            want = luv.desired_forwards(entry, rows, luv.ports_config(), {3007}, {})

        self.assertEqual(want[0]["local"], 3000)

    def _sync(self, sessions, detected, established=None, **kwargs):
        """Run a sync with the ssh work stubbed, returning what it asked for."""
        calls = []
        with patch.object(luv, "tunnel_up", return_value=True), \
             patch.object(luv, "port_free", return_value=True), \
             patch.object(luv, "load_tunnel_state", return_value=dict(established or {})), \
             patch.object(luv, "save_tunnel_state") as saved, \
             patch.object(luv, "tunnel_down") as down, \
             patch.object(luv, "forward_change",
                          side_effect=lambda hc, cfg, op, l, r: calls.append((op, l, r)) or True):
            fresh = luv.sync_forwards(sessions, detected, **kwargs)
        return calls, fresh, saved, down

    def _session(self, **kw):
        base = {"id": "s1", "host": "box", "workspace": "myrepo-box-42"}
        base.update(kw)
        return base

    @staticmethod
    def _row(port, label="web", workspace="myrepo-box-42"):
        return {"id": "", "workspace": workspace, "session": "luv-myrepo-box-42",
                "port": port, "label": label}

    def test_a_session_is_not_forwarded_until_it_is_asked_for(self):
        """A busy host carries dozens; they do not all get local ports uninvited."""
        calls, fresh, _, _ = self._sync([self._session()],
                                        {"box": [self._row(3000)]})

        self.assertEqual(calls, [])
        self.assertEqual(fresh, [])

    def test_naming_a_session_forwards_it(self):
        calls, fresh, saved, _ = self._sync(
            [self._session()], {"box": [self._row(3000)]},
            opt_in=lambda s: True)

        self.assertEqual(calls, [("forward", 3000, 3000)])
        self.assertEqual(fresh[0][1], [{"remote": 3000, "local": 3000, "label": "web"}])
        saved.assert_called_once()

    def test_a_session_that_holds_forwards_keeps_being_maintained(self):
        """This is what lets a forward outlive detaching."""
        session = self._session(
            forwards=[{"remote": 3000, "local": 3000, "label": "web"}])

        calls, _, _, _ = self._sync([session],
                                    {"box": [self._row(3000), self._row(5173, "vite")]},
                                    established={3000: 3000})

        self.assertEqual(calls, [("forward", 5173, 5173)])

    def test_a_server_that_stopped_has_its_forward_cancelled(self):
        session = self._session(forwards=[
            {"remote": 3000, "local": 3000, "label": "web"},
            {"remote": 5173, "local": 5173, "label": "vite"}])

        calls, _, _, _ = self._sync([session], {"box": [self._row(3000)]},
                                    established={3000: 3000, 5173: 5173})

        self.assertEqual(calls, [("cancel", 5173, 5173)])

    def test_an_unreachable_host_keeps_every_forward_it_has(self):
        """Merely losing the network must not read as 'the servers all stopped'."""
        session = self._session(
            forwards=[{"remote": 3000, "local": 3000, "label": "web"}])

        calls, fresh, saved, down = self._sync([session], {"box": None},
                                               established={3000: 3000})

        self.assertEqual(calls, [])
        self.assertEqual(fresh, [])
        down.assert_not_called()
        self.assertEqual(session["forwards"],
                         [{"remote": 3000, "local": 3000, "label": "web"}])

    def test_two_sessions_wanting_the_same_port_do_not_collide(self):
        a = self._session(id="s1", workspace="a-box-1")
        b = self._session(id="s2", workspace="b-box-2")
        detected = {"box": [self._row(3000, workspace="a-box-1"),
                            self._row(3000, workspace="b-box-2")]}

        calls, _, _, _ = self._sync([a, b], detected, opt_in=lambda s: True)

        self.assertEqual(sorted(calls), [("forward", 3000, 3000),
                                         ("forward", 3001, 3000)])
        self.assertNotEqual(a["forwards"][0]["local"], b["forwards"][0]["local"])

    def test_dropping_the_last_forward_closes_the_connection(self):
        session = self._session(
            forwards=[{"remote": 3000, "local": 3000, "label": "web"}])

        calls, _, saved, down = self._sync([session], {"box": []},
                                           established={3000: 3000})

        self.assertEqual(calls, [("cancel", 3000, 3000)])
        down.assert_called_once()
        saved.assert_not_called()

    def test_a_master_outliving_its_last_session_is_closed(self):
        """Otherwise it lingers until the machine reboots, holding dead forwards."""
        calls, _, saved, down = self._sync([], {"box": []},
                                           established={3000: 3000})

        self.assertEqual(calls, [("cancel", 3000, 3000)])
        down.assert_called_once()

    def test_an_unreachable_host_keeps_its_master(self):
        calls, _, _, down = self._sync([], {"box": None},
                                       established={3000: 3000})

        self.assertEqual(calls, [])
        down.assert_not_called()

    def test_a_local_session_is_never_tunnelled(self):
        """The servers are already on this machine."""
        calls, fresh, _, _ = self._sync(
            [self._session(host=None)],
            {"": [self._row(3000)]}, opt_in=lambda s: True)

        self.assertEqual(calls, [])
        self.assertEqual(fresh, [])

    def test_a_refused_forward_is_reported_once_per_host(self):
        refused = _completed("", 255, "channel setup failed: administratively prohibited")
        cfg = luv.ports_config()
        err = io.StringIO()

        with patch.object(luv, "tunnel_ctl", return_value=refused), \
             contextlib.redirect_stderr(err):
            self.assertFalse(luv.forward_change(self.HC, cfg, "forward", 1, 2))
            self.assertFalse(luv.forward_change(self.HC, cfg, "forward", 3, 4))

        self.assertEqual(err.getvalue().count("AllowTcpForwarding"), 1)

    def test_the_watcher_never_writes_to_the_terminal(self):
        """It runs under a full-screen agent UI; stdout there is somebody's redraw."""
        out, err = io.StringIO(), io.StringIO()

        with patch.object(luv, "ports_config", return_value=dict(luv.PORT_DEFAULTS)), \
             contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            luv.start_port_watcher(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
            time.sleep(0.05)

        self.assertEqual(out.getvalue(), "")
        self.assertEqual(err.getvalue(), "")

    def test_announcing_uses_tmux_rather_than_the_terminal(self):
        sent = []
        forwards = [{"remote": 3000, "local": 3001, "label": "dashboard"}]

        with patch.object(luv, "run", side_effect=lambda argv, **kw: sent.append(argv) or _completed()):
            luv.announce_forwards(self.HC, "luv-myrepo-box-42", forwards, forwards)

        joined = " ".join(" ".join(argv) for argv in sent)
        self.assertIn("set-option", joined)
        self.assertIn("@luv_ports", joined)
        self.assertIn("display-message", joined)
        self.assertIn("localhost:3001", joined)

    def test_nothing_new_means_nothing_to_announce(self):
        sent = []
        forwards = [{"remote": 3000, "local": 3001, "label": "dashboard"}]

        with patch.object(luv, "run", side_effect=lambda argv, **kw: sent.append(argv) or _completed()):
            luv.announce_forwards(self.HC, "luv-myrepo-box-42", forwards, [])

        joined = " ".join(" ".join(argv) for argv in sent)
        self.assertNotIn("display-message", joined)

    def test_the_ports_column_stays_short(self):
        session = {"ports": [3000, 5173, 6379, 8080, 8123, 9000]}

        self.assertEqual(luv.ports_cell(session), "3000,5173,6379,8080,+2")
        self.assertEqual(luv.ports_cell({}), "-")

    def test_ls_columns_still_line_up_with_ports_in_them(self):
        rows = [{"host": "box", "session": "luv-a", "workspace": "a-box-1",
                 "agent": "claude", "attached": True, "live": True,
                 "ports": [3000], "prompt": "fix it"},
                {"host": "box", "session": "luv-longer-name", "workspace": "b-box-2",
                 "agent": "codex", "attached": False, "live": True,
                 "prompt": "add limits"}]
        out = io.StringIO()

        with patch.object(luv.sys.stdout, "isatty", return_value=False), \
             contextlib.redirect_stdout(out):
            luv.print_sessions(rows)
        lines = out.getvalue().splitlines()

        header = lines[0]
        self.assertIn("PORTS", header)
        self.assertLess(header.index("PR "), header.index("PORTS"))
        self.assertLess(header.index("PORTS"), header.index("PROMPT"))
        for line in lines[1:]:
            self.assertEqual(line.index("fix it") if "fix it" in line
                             else line.index("add limits"),
                             header.index("PROMPT"))


if __name__ == "__main__":
    unittest.main()
