import contextlib
import io
import subprocess
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
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tempdir.cleanup()

    def _dispatch(self, argv, env=None):
        with (patch.object(sys, "argv", ["luv"] + argv),
              patch.dict(luv.os.environ, env or {}, clear=False),
              patch.object(luv.shutil, "which", side_effect=lambda n: f"/bin/{n}"),
              patch.object(luv.os, "execv") as execv,
              contextlib.redirect_stdout(io.StringIO()),
              contextlib.redirect_stderr(io.StringIO())):
            luv.main()
        return execv.call_args.args[1] if execv.called else None

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


if __name__ == "__main__":
    unittest.main()
