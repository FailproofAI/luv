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

    def _dispatch(self, argv, env=None, where=None):
        """Dispatch and return the argv handed to ssh.

        `where` is what the remote's 'luv --where' answers; None makes the host
        look unreachable, which is what drives the luv-pending fallback.
        """
        answer = _completed(f"{where}\n") if where else _completed(returncode=255)
        with (patch.object(sys, "argv", ["luv"] + argv),
              patch.dict(luv.os.environ, env or {}, clear=False),
              patch.object(luv, "ssh_run", return_value=answer),
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

    def test_pr_url_derives_session_from_url(self):
        argv = self._dispatch(["-l", "https://github.com/other/thing/pull/7"],
                              where="thing-box-7")

        self.assertIn("-s luv-thing-box-7", argv[-1])

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


if __name__ == "__main__":
    unittest.main()
