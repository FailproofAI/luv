import contextlib
import json
import os
import random
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LUV_DIR = Path.home() / ".luv"
CONFIG_FILE = LUV_DIR / "config.json"
SESSIONS_FILE = LUV_DIR / "sessions.json"
SESSIONS_LOCK = LUV_DIR / "sessions.lock"
CLAUDE_JSON = Path.home() / ".claude.json"
CLAUDE_SETTINGS_JSON = Path.home() / ".claude" / "settings.json"
CODEX_AGENTS_MD = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "AGENTS.md"

COLORS = ["red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan", "default"]


def pick_color() -> str:
    """Pick a random /color value so each luv session is visually distinct."""
    return random.choice(COLORS)

PR_RULES = """
# Pull Request Management

One PR per folder. Each folder maps to exactly one PR — create it once, then keep updating it across subsequent tasks.

## Rules

- Before creating a PR, check if one already exists for that folder (by title or branch name convention).
- If no PR exists for the folder: create one, then record its URL/number so it can be reused.
- If a PR already exists for the folder: push new commits to the same branch and do NOT open a new PR.
- PR titles should clearly identify the folder they cover (e.g. `[folder-name] ...`).
- Never open a second PR for the same folder — always update the existing one.
"""


def die(msg: str) -> None:
    print(f"luv: error: {msg}", file=sys.stderr)
    sys.exit(1)


def run(cmd: list[str], *, cwd: str | None = None,
        timeout: float | None = None) -> subprocess.CompletedProcess:
    """Capture a subprocess. A timeout surfaces as a failure, not an exception,
    so callers keep their plain returncode checks."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd,
                              timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout}s")


def load_config() -> dict:
    """Read ~/.luv/config.json, or return {} on missing/corrupt."""
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(data: dict) -> None:
    """Atomic-write config JSON to ~/.luv/config.json."""
    LUV_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(LUV_DIR), delete=False,
    ) as tmp:
        json.dump(data, tmp, indent=2)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, CONFIG_FILE)


def _resolve_prs_dir() -> Path:
    """Workspace root: _LUV_PRS_DIR env > config prs_dir > ~/prs.

    The env var is how the dispatcher tells a remote luv where to put clones;
    it is underscore-prefixed to stay out of collect_luv_env()'s LUV_* net.
    """
    env = os.environ.get("_LUV_PRS_DIR")
    if env:
        return Path(env).expanduser()
    configured = load_config().get("prs_dir")
    if isinstance(configured, str) and configured:
        return Path(configured).expanduser()
    return Path.home() / "prs"


PRS_DIR = _resolve_prs_dir()

# Host-config keys that may be overridden per host under remote.hosts.<name>.
HOST_KEYS = ("identity_file", "port", "dir", "luv_bin", "ssh_opts")


def resolve_host(explicit: str | None = None,
                 identity: str | None = None) -> dict | None:
    """Resolve the SSH target: -s flag > config remote.host. None when local.

    Merges remote.* defaults with any remote.hosts.<name> overrides, so a
    second host reached via -s gets its own key/port instead of inheriting
    settings that only make sense for the default one.
    """
    remote = load_config().get("remote")
    if not isinstance(remote, dict):
        remote = {}
    host = explicit or remote.get("host")
    if not host:
        return None

    hc = {"host": host}
    for key in HOST_KEYS:
        if remote.get(key) is not None:
            hc[key] = remote[key]
    overrides = remote.get("hosts")
    if isinstance(overrides, dict) and isinstance(overrides.get(host), dict):
        for key in HOST_KEYS:
            if overrides[host].get(key) is not None:
                hc[key] = overrides[host][key]
    if identity:
        hc["identity_file"] = identity
    return hc


def ssh_base(hc: dict, *, tty: bool = False, batch: bool = False) -> list[str]:
    """Build the ssh argv prefix for a host. Every SSH call site goes through
    here — an identity file that applied to some commands but not others would
    be worse than none at all."""
    cmd = ["ssh"]
    if tty:
        cmd.append("-t")
    if batch:
        cmd += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    if hc.get("identity_file"):
        # ssh does not expand ~ itself when argv arrives via execv, not a shell.
        cmd += ["-i", str(Path(str(hc["identity_file"])).expanduser())]
    if hc.get("port"):
        cmd += ["-p", str(hc["port"])]
    cmd += [str(o) for o in (hc.get("ssh_opts") or [])]
    return cmd + [str(hc["host"])]


def remote_shell(cmd: str) -> str:
    """Wrap a command for a remote login shell.

    ssh joins its post-host arguments with spaces and hands the result to the
    remote shell, so this must survive exactly one round of shell parsing. The
    login shell matters: ~/.profile is what puts ~/.local/bin (pip/uv installs
    of luv, claude, gh) on PATH, and non-login ssh shells routinely omit it.
    """
    return f"bash -lc {shlex.quote(cmd)}"


def ssh_run(hc: dict | None, remote_cmd: str, *, batch: bool = True):
    """Run a shell command on `hc`, or locally when hc is None."""
    if hc is None:
        return subprocess.run(remote_cmd, shell=True, capture_output=True, text=True)
    return run(ssh_base(hc, batch=batch) + [remote_shell(remote_cmd)])


def tmux_session_name(folder: str) -> str:
    """tmux forbids '.' and ':' in session names; repo names like foo.js don't."""
    return "luv-" + folder.replace(".", "_").replace(":", "_")


TMUX_LIST_CMD = (
    "tmux list-sessions -F "
    "'#{@luv_id}|#{session_name}|#{@luv_workspace}|#{session_attached}|#{session_activity}'"
    " 2>/dev/null"
)


def query_tmux(hc: dict | None) -> list[dict] | None:
    """List luv tmux sessions on a host, or None if the host never answered.

    The None case is load-bearing: callers must not prune the registry when a
    host is merely unreachable. ssh exits 255 on its own connection failures,
    which is what distinguishes that from "connected, but no sessions".
    """
    r = ssh_run(hc, TMUX_LIST_CMD)
    if hc is not None and r.returncode == 255:
        return None
    rows = []
    for line in r.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) != 5 or not parts[1].startswith("luv-"):
            continue
        luv_id, name, workspace, attached, activity = parts
        rows.append({
            "id": luv_id,
            "session": name,
            "workspace": workspace,
            "attached": attached == "1",
            "activity": int(activity) if activity.isdigit() else 0,
        })
    return rows


def live_tmux_sessions() -> set[str]:
    """Session names on this machine; empty set when tmux isn't running."""
    r = run(["tmux", "list-sessions", "-F", "#{session_name}"])
    if r.returncode != 0:
        return set()
    return {line.strip() for line in r.stdout.splitlines() if line.strip()}


def tmux_adopt(clone_dir: Path) -> None:
    """Rename the dispatcher's placeholder session and stamp identity onto it.

    The stamped @luv_id is how the laptop's registry finds this session again:
    it cannot know the workspace number up front (that comes from gh api here),
    so it records a token and matches on it afterwards. No-op outside tmux.
    """
    if not os.environ.get("TMUX"):
        return
    target = tmux_session_name(clone_dir.name)
    pending = os.environ.get("_LUV_TMUX_PENDING")
    if pending:
        if run(["tmux", "rename-session", "-t", pending, target]).returncode != 0:
            target = f"{target}-2"  # name already taken by another live session
            run(["tmux", "rename-session", "-t", pending, target])
            print(f"luv: warning: session name taken, using {target}", file=sys.stderr)
    run(["tmux", "set-option", "-t", target, "@luv_workspace", clone_dir.name])
    luv_id = os.environ.get("_LUV_ID")
    if luv_id:
        run(["tmux", "set-option", "-t", target, "@luv_id", luv_id])


# Recomputed on every reconcile; never written back to sessions.json.
TRANSIENT_KEYS = ("live", "attached", "activity")


def load_sessions() -> list[dict]:
    """Read ~/.luv/sessions.json, or return [] on missing/corrupt."""
    if not SESSIONS_FILE.exists():
        return []
    try:
        data = json.loads(SESSIONS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    sessions = data.get("sessions") if isinstance(data, dict) else None
    if not isinstance(sessions, list):
        return []
    return [s for s in sessions if isinstance(s, dict)]


def save_sessions(sessions: list[dict]) -> None:
    """Atomic-write the session registry, dropping recomputed fields."""
    LUV_DIR.mkdir(parents=True, exist_ok=True)
    clean = [{k: v for k, v in s.items() if k not in TRANSIENT_KEYS} for s in sessions]
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(LUV_DIR), delete=False,
    ) as tmp:
        json.dump({"sessions": clean}, tmp, indent=2)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, SESSIONS_FILE)


@contextlib.contextmanager
def session_lock(timeout: float = 5.0):
    """Guard the read-modify-write on sessions.json.

    The atomic replace in save_sessions protects a single write, not a
    read-modify-write: two luv invocations appending at once would lose one.
    Falls through unlocked on timeout — losing a registry entry beats refusing
    to launch an agent.
    """
    LUV_DIR.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd = None
    while True:
        try:
            fd = os.open(str(SESSIONS_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:  # break a lock left behind by a killed process
                if time.time() - SESSIONS_LOCK.stat().st_mtime > timeout:
                    SESSIONS_LOCK.unlink()
                    continue
            except OSError:
                pass
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
            try:
                SESSIONS_LOCK.unlink()
            except OSError:
                pass


def new_session_id() -> str:
    """Short token tying a registry entry to its tmux session's @luv_id."""
    return "".join(random.choice("0123456789abcdef") for _ in range(8))


def record_session(entry: dict) -> None:
    """Append (or replace) a registry entry under the lock."""
    with session_lock():
        sessions = [s for s in load_sessions() if s.get("id") != entry.get("id")]
        sessions.append(entry)
        save_sessions(sessions)


def reconcile(sessions: list[dict],
              identity: str | None = None) -> tuple[list[dict], set[str]]:
    """Refresh registry entries against live tmux state on each host.

    Returns (entries, unreachable_hosts). Entries whose host did not answer are
    kept and flagged rather than pruned — running `luv ls` on a plane must not
    wipe the registry.
    """
    hosts = sorted({s.get("host") or "" for s in sessions})
    if not hosts:
        return [], set()

    def probe(h: str) -> list[dict] | None:
        return query_tmux(resolve_host(h, identity) if h else None)

    if len(hosts) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(hosts))) as pool:
            results = dict(zip(hosts, pool.map(probe, hosts)))
    else:
        results = {hosts[0]: probe(hosts[0])}

    now = int(time.time())
    kept: list[dict] = []
    for s in sessions:
        live = results.get(s.get("host") or "")
        if live is None:  # host unreachable — keep last known state
            s["live"] = None
            kept.append(s)
            continue
        match = next((r for r in live if r["id"] and r["id"] == s.get("id")), None)
        if match is None:
            match = next((r for r in live if r["session"] == s.get("session")), None)
        if match is None:
            continue  # session is genuinely gone
        s["session"] = match["session"]
        s["workspace"] = match["workspace"] or s.get("workspace")
        s["attached"] = match["attached"]
        s["activity"] = match["activity"]
        s["last_seen"] = now
        s["live"] = True
        kept.append(s)
    return kept, {h for h, v in results.items() if v is None}


def parse_github_remote(cwd: str) -> tuple[str, str] | None:
    """Extract (org, repo) from origin remote URL. Returns None on failure."""
    r = run(["git", "remote", "get-url", "origin"], cwd=cwd)
    if r.returncode != 0:
        return None
    url = r.stdout.strip()
    m = re.match(r"https://github\.com/([^/]+)/([^/.]+)", url)
    if not m:
        m = re.match(r"git@github\.com:([^/]+)/([^/.]+)", url)
    if m:
        return m.group(1), m.group(2)
    return None


def workspace_number(name: str | None) -> int | None:
    """The trailing N of a '{repo}-{N}' workspace folder, or None if it isn't one."""
    if not name:
        return None
    parts = name.rsplit("-", 1)
    if len(parts) != 2 or not parts[1].isdigit():
        return None
    return int(parts[1])


def resolve_org(explicit: str | None = None) -> str:
    """Resolve GitHub org: explicit arg > config file > error."""
    if explicit:
        return explicit
    cfg = load_config()
    org = cfg.get("org")
    if org:
        return org
    die("no default org configured.\nRun 'luv --init' to set one, or use 'org/repo' syntax.")
    return ""  # unreachable, keeps type checkers happy


def trust_project(path: Path) -> None:
    data: dict[str, object] = {}
    if CLAUDE_JSON.exists():
        try:
            with CLAUDE_JSON.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                data = loaded
        except (json.JSONDecodeError, OSError):
            data = {}

    projects = data.get("projects")
    if not isinstance(projects, dict):
        projects = {}
        data["projects"] = projects

    entry = projects.get(str(path))
    if not isinstance(entry, dict):
        entry = {}
        projects[str(path)] = entry

    entry["hasTrustDialogAccepted"] = True
    CLAUDE_JSON.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(CLAUDE_JSON.parent),
        delete=False,
    ) as tmp:
        json.dump(data, tmp, indent=2)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, CLAUDE_JSON)


def collect_luv_env() -> dict[str, str]:
    """Collect LUV_* env vars, strip prefix, return as dict."""
    result = {}
    for key, value in os.environ.items():
        if key.startswith("LUV_") and len(key) > 4:
            result[key[4:]] = value
    return result


def docker_env_flags(env_vars: dict[str, str]) -> list[str]:
    """Convert env dict to docker compose exec -e flags."""
    flags: list[str] = []
    for key, value in env_vars.items():
        flags.extend(["-e", f"{key}={value}"])
    return flags


def ensure_pr_rules(agent: str = "claude") -> None:
    """Install the workspace/PR convention for the selected agent."""
    instructions_file = (Path.home() / ".claude" / "CLAUDE.md"
                         if agent == "claude" else CODEX_AGENTS_MD)
    instructions_file.parent.mkdir(parents=True, exist_ok=True)
    existing = instructions_file.read_text() if instructions_file.exists() else ""
    if "# Pull Request Management" not in existing:
        with instructions_file.open("a") as f:
            f.write(PR_RULES)


def ensure_default_permission_mode() -> None:
    """Set permissions.defaultMode = bypassPermissions in ~/.claude/settings.json.

    Merges into existing JSON without clobbering other keys. No-op if the file
    exists but is unreadable/invalid, or if the value is already set.
    """
    data: dict[str, object] = {}
    if CLAUDE_SETTINGS_JSON.exists():
        try:
            with CLAUDE_SETTINGS_JSON.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            if not isinstance(loaded, dict):
                return
            data = loaded
        except (json.JSONDecodeError, OSError):
            return

    permissions = data.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
        data["permissions"] = permissions

    if permissions.get("defaultMode") == "bypassPermissions":
        return
    permissions["defaultMode"] = "bypassPermissions"

    CLAUDE_SETTINGS_JSON.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=str(CLAUDE_SETTINGS_JSON.parent),
        delete=False,
    ) as tmp:
        json.dump(data, tmp, indent=2)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, CLAUDE_SETTINGS_JSON)


def pick_org() -> str:
    """Interactive GitHub owner picker. Returns the selected org or username."""
    r = run(["gh", "api", "user", "--jq", ".login"])
    if r.returncode != 0:
        die("'gh' not found or not authenticated. Run 'gh auth login' first.")
    username = r.stdout.strip()

    r = run(["gh", "api", "user/orgs", "--jq", ".[].login"])
    orgs = [line for line in r.stdout.strip().splitlines() if line] if r.returncode == 0 else []

    choices = [f"{username} (personal)"] + orgs
    print("luv: select default GitHub owner:")
    for i, name in enumerate(choices, 1):
        print(f"  {i}) {name}")
    other_idx = len(choices) + 1
    print(f"  {other_idx}) other (type manually)")

    raw = input(f"Choice [1]: ").strip()
    if not raw:
        idx = 1
    else:
        try:
            idx = int(raw)
        except ValueError:
            die(f"invalid choice: '{raw}'")

    if idx == other_idx:
        selected = input("GitHub org or username: ").strip()
        if not selected:
            die("no org entered")
    elif 1 <= idx <= len(choices):
        selected = choices[idx - 1].split(" (")[0]  # strip " (personal)" suffix
    else:
        die(f"invalid choice: {idx}")

    return selected


def cmd_init() -> None:
    """Interactive setup: choose a default GitHub org."""
    if not sys.stdin.isatty():
        die("--init requires an interactive terminal")
    selected = pick_org()
    config = load_config()
    config["org"] = selected
    save_config(config)
    print(f"luv: default org set to '{selected}'. Saved to ~/.luv/config.json")


_MISSING = object()


def config_get(data: dict, key: str):
    """Look up a dotted key (remote.hosts.gpu.port) in nested config."""
    cur = data
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def config_set(data: dict, key: str, value) -> None:
    """Assign a dotted key, creating intermediate dicts as needed."""
    parts = key.split(".")
    cur = data
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


def config_unset(data: dict, key: str) -> bool:
    """Remove a dotted key. Returns False if it wasn't set."""
    parts = key.split(".")
    cur = data
    for part in parts[:-1]:
        cur = cur.get(part)
        if not isinstance(cur, dict):
            return False
    return cur.pop(parts[-1], _MISSING) is not _MISSING


def config_flatten(data: dict, prefix: str = "") -> list[tuple[str, object]]:
    """Flatten nested config to sorted (dotted_key, value) pairs."""
    out: list[tuple[str, object]] = []
    for key, value in sorted(data.items()):
        full = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out.extend(config_flatten(value, f"{full}."))
        else:
            out.append((full, value))
    return out


def default_identity() -> str | None:
    """First conventional SSH key present, for the wizard's default."""
    for name in ("id_ed25519", "id_rsa", "id_ecdsa"):
        if (Path.home() / ".ssh" / name).exists():
            return f"~/.ssh/{name}"
    return None


def preflight_host(hc: dict) -> None:
    """Verify the remote has what luv needs, and say precisely what's missing.

    Worth the round trip: without it, a missing remote binary surfaces much
    later as an opaque failure in the middle of an ssh+tmux handoff.
    """
    print(f"luv: checking {hc['host']}...")
    r = ssh_run(hc, "for b in tmux luv gh git; do command -v $b >/dev/null || echo $b; done")
    if r.returncode == 255:
        detail = r.stderr.strip().splitlines()
        print(f"luv: warning: cannot connect to {hc['host']}", file=sys.stderr)
        if detail:
            print(f"  {detail[-1]}", file=sys.stderr)
        if hc.get("identity_file"):
            print(f"  tried identity file {hc['identity_file']}", file=sys.stderr)
        else:
            print("  no identity file configured — set one with "
                  "'luv config set remote.identity_file <path>'", file=sys.stderr)
        return
    missing = r.stdout.split()
    if not missing:
        print(f"luv: {hc['host']} ready (tmux, luv, gh, git present)")
        return
    print(f"luv: warning: missing on {hc['host']}: {', '.join(missing)}", file=sys.stderr)
    if "luv" in missing:
        print("  install it there ('uv tool install luv-cli' or 'pip install luv-cli'), "
              "or set remote.luv_bin to its full path", file=sys.stderr)
    if "gh" in missing:
        print("  luv needs an authenticated 'gh' on the remote to number workspaces",
              file=sys.stderr)


def cmd_config(args: list[str]) -> None:
    """luv config [set|get|list|unset]; no verb runs the interactive wizard."""
    verb = args[0] if args else None

    if verb == "list":
        entries = config_flatten(load_config())
        if not entries:
            print("luv: no configuration set (run 'luv config')")
            return
        width = max(len(key) for key, _ in entries)
        for key, value in entries:
            print(f"{key.ljust(width)}  {json.dumps(value)}")
        return

    if verb == "get":
        if len(args) != 2:
            die("usage: luv config get <key>")
        value = config_get(load_config(), args[1])
        if value is _MISSING:
            die(f"'{args[1]}' is not set")
        print(value if isinstance(value, str) else json.dumps(value))
        return

    if verb == "set":
        if len(args) != 3:
            die("usage: luv config set <key> <value>")
        config = load_config()
        try:
            value = json.loads(args[2])  # numbers, booleans, lists
        except json.JSONDecodeError:
            value = args[2]              # plain string
        config_set(config, args[1], value)
        save_config(config)
        print(f"luv: {args[1]} = {json.dumps(value)}")
        return

    if verb == "unset":
        if len(args) != 2:
            die("usage: luv config unset <key>")
        config = load_config()
        if not config_unset(config, args[1]):
            die(f"'{args[1]}' is not set")
        save_config(config)
        print(f"luv: unset {args[1]}")
        return

    if verb is not None:
        die(f"unknown config command '{verb}' (expected set, get, list, unset)")

    if not sys.stdin.isatty():
        die("'luv config' needs an interactive terminal; use 'luv config set <key> <value>'")

    config = load_config()
    remote = config.get("remote")
    if not isinstance(remote, dict):
        remote = {}

    current = remote.get("host") or ""
    host = input(f"Remote host (ssh alias or user@host) [{current or 'none'}]: ").strip()
    if not host:
        host = current
    if host:
        remote["host"] = host
        suggested = remote.get("identity_file") or default_identity()
        key = input(f"SSH identity file [{suggested or 'ssh default'}]: ").strip() or suggested
        if key:
            remote["identity_file"] = key
        wsdir = input(f"Remote workspace dir [{remote.get('dir') or '~/prs'}]: ").strip()
        if wsdir:
            remote["dir"] = wsdir
        config["remote"] = remote
    else:
        config.pop("remote", None)
        print("luv: no remote host — luv will run locally")
    save_config(config)

    if input("Configure default GitHub org? [Y/n]: ").strip().lower() not in ("n", "no"):
        config = load_config()
        config["org"] = pick_org()
        save_config(config)

    print(f"luv: saved to {CONFIG_FILE}")
    if host:
        hc = resolve_host(host)
        if hc:
            preflight_host(hc)


def load_luv_settings(clone_dir: Path) -> dict | None:
    """Read .luv/settings.json from the repo, or return None."""
    settings_file = clone_dir / ".luv" / "settings.json"
    if not settings_file.exists():
        return None
    try:
        return json.loads(settings_file.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def docker_project_name(clone_dir: Path) -> str:
    """Unique Compose project name — scopes networks and volumes."""
    return f"luv-{clone_dir.name}"


def docker_compose_base(clone_dir: Path, compose_file: str, project: str) -> list[str]:
    """Base docker compose command with project directory and file."""
    return ["docker", "compose", "-f", str(clone_dir / compose_file),
            "--project-directory", str(clone_dir), "-p", project]


def start_docker(clone_dir: Path, compose_file: str, project: str) -> None:
    """Start a fresh Docker Compose environment with isolated network/volumes."""
    compose_path = clone_dir / compose_file
    if not compose_path.exists():
        die(f"compose file not found: {compose_file}")

    base = docker_compose_base(clone_dir, compose_file, project)

    # Tear down stale environment (ignore errors if nothing exists)
    subprocess.run(base + ["down", "-v", "--remove-orphans"], capture_output=True)

    # Start fresh
    print(f"luv: starting docker environment ({project})...")
    r = subprocess.run(base + ["up", "-d", "--build"])
    if r.returncode != 0:
        die("docker compose up failed")

    # Verify dev-environment service is running
    r = subprocess.run(base + ["ps", "--format", "json", "dev-environment"],
                       capture_output=True, text=True)
    if r.returncode != 0 or "running" not in r.stdout.lower():
        subprocess.run(base + ["logs", "dev-environment"])
        die("'dev-environment' service is not running")

    print("luv: docker environment ready")


def stop_docker(clone_dir: Path, compose_file: str, project: str) -> None:
    """Tear down Docker Compose environment, removing volumes and orphans."""
    base = docker_compose_base(clone_dir, compose_file, project)
    print(f"luv: tearing down docker environment ({project})...")
    subprocess.run(base + ["down", "-v", "--remove-orphans"])


def navigate(clone_dir: Path, extra_env: dict[str, str] = {}) -> None:
    """Chdir into the work folder and exec a shell — replacing this process."""
    tmux_adopt(clone_dir)
    os.chdir(str(clone_dir))
    settings = load_luv_settings(clone_dir)
    compose_file = (settings or {}).get("compose_file")

    if compose_file:
        project = docker_project_name(clone_dir)
        start_docker(clone_dir, compose_file, project)
        try:
            base = docker_compose_base(clone_dir, compose_file, project)
            r = subprocess.run(base + ["exec", "-it"] + docker_env_flags(extra_env) + ["dev-environment", "bash"])
            sys.exit(r.returncode)
        finally:
            stop_docker(clone_dir, compose_file, project)
    else:
        shell = os.environ.get("SHELL", "/bin/bash")
        os.environ.update(extra_env)
        os.execv(shell, [shell])


def resume(clone_dir: Path, extra_env: dict[str, str] | None = None,
           model: str | None = None, agent: str = "claude") -> None:
    """Chdir and resume the selected agent, replacing this process."""
    extra_env = extra_env or {}
    tmux_adopt(clone_dir)
    if agent == "claude":
        trust_project(clone_dir)
    os.chdir(str(clone_dir))
    settings = load_luv_settings(clone_dir)
    compose_file = (settings or {}).get("compose_file")

    if agent == "claude":
        agent_cmd = ["claude", "--dangerously-skip-permissions",
                     "--model", model or "claude-opus-5",
                     "--effort", "max", "--resume",
                     "--remote-control",
                     "--remote-control-session-name-prefix", clone_dir.name]
    else:
        agent_cmd = ["codex", "resume", "--last",
                     "--dangerously-bypass-approvals-and-sandbox"]
        if model:
            agent_cmd += ["--model", model]

    if compose_file:
        project = docker_project_name(clone_dir)
        start_docker(clone_dir, compose_file, project)
        try:
            base = docker_compose_base(clone_dir, compose_file, project)
            r = subprocess.run(base + ["exec", "-it"] + docker_env_flags(extra_env)
                               + ["dev-environment"] + agent_cmd)
            sys.exit(r.returncode)
        finally:
            stop_docker(clone_dir, compose_file, project)
    else:
        agent_bin = shutil.which(agent)
        if not agent_bin:
            die(f"'{agent}' not found in PATH")
        os.environ.update(extra_env)
        os.execv(agent_bin, [agent_bin] + agent_cmd[1:])


def launch(clone_dir: Path, prompt: str | None, plan_mode: bool = False,
           non_interactive: bool = False, extra_env: dict[str, str] | None = None,
           model: str | None = None, agent: str = "claude") -> None:
    """Resolve and launch the selected agent, replacing this process."""
    extra_env = extra_env or {}
    tmux_adopt(clone_dir)
    if agent == "claude":
        trust_project(clone_dir)
    os.chdir(str(clone_dir))
    settings = load_luv_settings(clone_dir)
    compose_file = (settings or {}).get("compose_file")

    if agent == "codex":
        if plan_mode:
            die("-p is only supported with Claude")
        agent_cmd = ["codex"]
        if non_interactive:
            if not prompt:
                die("-nit requires a prompt")
            agent_cmd += ["exec", "--dangerously-bypass-approvals-and-sandbox"]
        else:
            agent_cmd += ["--dangerously-bypass-approvals-and-sandbox"]
        if model:
            agent_cmd += ["--model", model]
        if prompt:
            agent_cmd.append(prompt)
    else:
        common_flags = ["--dangerously-skip-permissions",
                    "--model", model or "claude-opus-5",
                    "--effort", "max",
                    "--remote-control",
                    "--remote-control-session-name-prefix", clone_dir.name]
        if non_interactive:
            if not prompt:
                die("-nit requires a prompt")
            mode_flags = ["--output-format", "stream-json",
                          "--verbose", "--include-partial-messages"]
            initial_args = ["-p", prompt]
        elif plan_mode:
            mode_flags = ["--permission-mode", "plan"]
            initial_args = [prompt] if prompt else [f"/color {pick_color()}"]
        else:
            mode_flags = ["--permission-mode", "bypassPermissions"]
            initial_args = [prompt] if prompt else [f"/color {pick_color()}"]
        agent_cmd = ["claude"] + common_flags + mode_flags + initial_args

    if compose_file:
        project = docker_project_name(clone_dir)
        start_docker(clone_dir, compose_file, project)
        try:
            base = docker_compose_base(clone_dir, compose_file, project)
            r = subprocess.run(base + ["exec", "-it"] + docker_env_flags(extra_env)
                               + ["dev-environment"] + agent_cmd)
            sys.exit(r.returncode)
        finally:
            stop_docker(clone_dir, compose_file, project)
    else:
        agent_bin = shutil.which(agent)
        if not agent_bin:
            die(f"'{agent}' not found in PATH")
        os.environ.update(extra_env)
        os.execv(agent_bin, [agent_bin] + agent_cmd[1:])


SAFE_AGE_SECONDS = 24 * 3600


def _on_rm_error(func, path, _exc):
    """rmtree handler: make `path` (and its parent dir) writable, then retry."""
    parent = os.path.dirname(path)
    try:
        os.chmod(parent, os.stat(parent).st_mode | stat.S_IWUSR | stat.S_IXUSR)
    except OSError:
        pass
    os.chmod(path, stat.S_IWUSR | stat.S_IRUSR | stat.S_IXUSR)
    func(path)


def _docker_wipe(path: Path) -> bool:
    """rm -rf `path` from inside a busybox container so the container's root can
    delete files that a previous container bind-mounted in as root (e.g. Rust
    target dirs built inside the workspace's dev-environment). Returns True on
    success. No-op if docker isn't available."""
    if shutil.which("docker") is None:
        return False
    parent = path.resolve().parent
    name = path.name
    r = subprocess.run(
        ["docker", "run", "--rm",
         "-v", f"{parent}:/p",
         "busybox", "rm", "-rf", f"/p/{name}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        sys.stderr.write(f"luv: docker rm fallback failed for {path}: {r.stderr.strip()}\n")
        return False
    return not path.exists()


def _force_rmtree(path: Path) -> None:
    """rmtree that survives read-only files (chmod-and-retry) and root-owned
    files left behind by Docker bind-mounts (containerized rm -rf fallback)."""
    kwargs = {"onexc": _on_rm_error} if sys.version_info >= (3, 12) else {"onerror": _on_rm_error}
    try:
        shutil.rmtree(path, **kwargs)
    except PermissionError:
        if not _docker_wipe(path):
            raise


def cmd_clean(force: bool = False, safe: bool = False) -> None:
    """Scan ~/prs/ and delete fully-pushed, clean work folders."""
    if not PRS_DIR.exists():
        print("luv: nothing to clean (~/prs/ does not exist)")
        return

    cleaned: list[str] = []
    skipped: list[tuple[str, str]] = []
    now = time.time()
    # Persistent tmux sessions make "delete a folder someone is working in" a
    # real hazard rather than a theoretical one.
    live = set() if force else live_tmux_sessions()

    for entry in sorted(PRS_DIR.iterdir()):
        if not entry.is_dir():
            continue

        number = workspace_number(entry.name)
        if number is None:
            continue  # doesn't match {repo}-{number} — skip silently

        if tmux_session_name(entry.name) in live:
            skipped.append((entry.name, "live tmux session"))
            continue

        if force:
            if safe and (now - entry.stat().st_mtime) < SAFE_AGE_SECONDS:
                skipped.append((entry.name, "younger than 24h (--safe)"))
                continue
            _force_rmtree(entry)
            cleaned.append(entry.name)
            continue

        branch = f"luv-{number}"
        cwd = str(entry)

        # Must be a git repo
        if run(["git", "rev-parse", "--git-dir"], cwd=cwd).returncode != 0:
            continue

        # 1. Working tree must be clean
        r = run(["git", "status", "--porcelain"], cwd=cwd)
        if r.returncode != 0 or r.stdout.strip():
            skipped.append((entry.name, "uncommitted changes"))
            continue

        # 2. Fetch remote branch; if gone, check for a merged PR
        fetch_ok = run(["git", "fetch", "origin", branch], cwd=cwd).returncode == 0

        if not fetch_ok:
            remote_info = parse_github_remote(cwd)
            if remote_info is None:
                skipped.append((entry.name, "cannot determine org from git remote"))
                continue
            remote_org, repo_name = remote_info
            r = run(["gh", "api", f"repos/{remote_org}/{repo_name}/pulls",
                     "-f", "state=closed", "-f", f"head={remote_org}:{branch}",
                     "-f", "per_page=5"])
            if r.returncode != 0:
                skipped.append((entry.name, "branch not on remote"))
                continue
            prs = json.loads(r.stdout)
            merged = [pr for pr in prs if pr.get("merged_at")]
            if not merged:
                skipped.append((entry.name, "branch not on remote"))
                continue
            pr_head_sha = merged[0]["head"]["sha"]
            local_sha = run(["git", "rev-parse", "HEAD"], cwd=cwd).stdout.strip()
            if local_sha != pr_head_sha:
                skipped.append((entry.name, "local HEAD differs from merged PR head"))
                continue
            _force_rmtree(entry)
            cleaned.append(entry.name)
            continue

        # 3. No unpushed commits (branch still exists on remote)
        r = run(["git", "rev-list", f"origin/{branch}..HEAD", "--count"], cwd=cwd)
        if r.returncode != 0 or r.stdout.strip() != "0":
            skipped.append((entry.name, "unpushed commits"))
            continue

        _force_rmtree(entry)
        cleaned.append(entry.name)

    if skipped:
        print("luv: skipped (not clean):")
        for name, reason in skipped:
            print(f"  {name}: {reason}")

    if cleaned:
        print("luv: cleaned:")
        for name in cleaned:
            print(f"  {name}")

    if not skipped and not cleaned:
        print("luv: nothing to clean")


def find_latest_clone(repo: str) -> Path | None:
    """Return the highest-numbered local {repo}-{N} folder, or None."""
    if not PRS_DIR.exists():
        return None
    best: Path | None = None
    best_num = -1
    for entry in PRS_DIR.iterdir():
        if not entry.is_dir():
            continue
        parts = entry.name.rsplit("-", 1)
        if len(parts) == 2 and parts[0] == repo and parts[1].isdigit():
            n = int(parts[1])
            if n > best_num:
                best, best_num = entry, n
    return best


def open_existing(org: str, repo: str, number: int, prompt: str | None, nav_mode: bool = False, resume_mode: bool = False, plan_mode: bool = False, non_interactive: bool = False, extra_env: dict[str, str] | None = None, model: str | None = None, agent: str = "claude") -> None:
    """Open an existing work folder or remote branch by number."""
    extra_env = extra_env or {}
    clone_dir = PRS_DIR / f"{repo}-{number}"

    # 1. Local folder takes priority
    if clone_dir.exists():
        print(f"luv: opening existing folder {clone_dir.name}")
        ensure_pr_rules(agent)
        if nav_mode:
            navigate(clone_dir, extra_env=extra_env)
        elif resume_mode:
            resume(clone_dir, extra_env=extra_env, model=model, agent=agent)
        else:
            launch(clone_dir, prompt, plan_mode=plan_mode, non_interactive=non_interactive, extra_env=extra_env, model=model, agent=agent)
        return  # unreachable

    # 2. Check remote branch luv-{number}
    branch = f"luv-{number}"
    clone_url = f"https://github.com/{org}/{repo}"
    r = run(["git", "ls-remote", "--heads", clone_url, branch])
    if branch not in r.stdout:
        die(f"no local folder '{repo}-{number}' and no remote branch '{branch}'")

    # 3. Clone and checkout the existing branch
    PRS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"luv: cloning {clone_url} -> {clone_dir} (branch {branch})")
    r = subprocess.run(["git", "clone", clone_url, str(clone_dir)])
    if r.returncode != 0:
        die(f"git clone failed (exit {r.returncode})")
    r = subprocess.run(["git", "checkout", branch], cwd=str(clone_dir))
    if r.returncode != 0:
        die(f"git checkout {branch} failed (exit {r.returncode})")

    print(f"luv: ready — {clone_dir.name}, branch {branch}")
    ensure_pr_rules(agent)
    if nav_mode:
        navigate(clone_dir, extra_env=extra_env)
    elif resume_mode:
        resume(clone_dir, extra_env=extra_env, model=model, agent=agent)
    else:
        launch(clone_dir, prompt, plan_mode=plan_mode, non_interactive=non_interactive, extra_env=extra_env, model=model, agent=agent)


def open_pr(org: str, repo: str, number: int, prompt: str | None, nav_mode: bool = False, resume_mode: bool = False, plan_mode: bool = False, non_interactive: bool = False, extra_env: dict[str, str] | None = None, model: str | None = None, agent: str = "claude") -> None:
    """Open any GitHub PR by org/repo/number, cloning if needed."""
    extra_env = extra_env or {}
    clone_dir = PRS_DIR / f"{repo}-{number}"

    if clone_dir.exists():
        print(f"luv: opening existing folder {clone_dir.name}")
        ensure_pr_rules(agent)
        if nav_mode:
            navigate(clone_dir, extra_env=extra_env)
        elif resume_mode:
            resume(clone_dir, extra_env=extra_env, model=model, agent=agent)
        else:
            launch(clone_dir, prompt, plan_mode=plan_mode, non_interactive=non_interactive, extra_env=extra_env, model=model, agent=agent)
        return  # unreachable

    # Resolve the actual branch name via GitHub API
    r = run(["gh", "api", f"repos/{org}/{repo}/pulls/{number}"])
    if r.returncode != 0:
        die(f"PR {org}/{repo}#{number} not found.\n{r.stderr.strip()}")
    pr_data = json.loads(r.stdout)
    branch = pr_data["head"]["ref"]
    clone_url = pr_data["head"]["repo"]["clone_url"]

    PRS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"luv: cloning {clone_url} -> {clone_dir} (branch {branch})")
    r = subprocess.run(["git", "clone", clone_url, str(clone_dir)])
    if r.returncode != 0:
        die(f"git clone failed (exit {r.returncode})")
    r = subprocess.run(["git", "checkout", branch], cwd=str(clone_dir))
    if r.returncode != 0:
        die(f"git checkout {branch} failed (exit {r.returncode})")

    print(f"luv: ready — {clone_dir.name}, branch {branch}")
    ensure_pr_rules(agent)
    if nav_mode:
        navigate(clone_dir, extra_env=extra_env)
    elif resume_mode:
        resume(clone_dir, extra_env=extra_env, model=model, agent=agent)
    else:
        launch(clone_dir, prompt, plan_mode=plan_mode, non_interactive=non_interactive, extra_env=extra_env, model=model, agent=agent)


def exec_ssh(hc: dict, remote_cmd: str, *, tty: bool = True) -> None:
    """Hand the terminal to ssh, replacing this process.

    execv (rather than subprocess.run) is what gives correct TTY handling,
    Ctrl-C, and exit-code passthrough for free — same as the local agent paths.
    """
    ssh_bin = shutil.which("ssh")
    if not ssh_bin:
        die("'ssh' not found in PATH")
    argv = ssh_base(hc, tty=tty) + [remote_shell(remote_cmd)]
    os.execv(ssh_bin, [ssh_bin] + argv[1:])


def attach_session(hc: dict | None, name: str) -> None:
    """Attach to a tmux session, locally or over ssh. Replaces this process.

    -d detaches other clients so the pane isn't size-clamped to a stale window
    left open elsewhere; these are all the same user's sessions.
    """
    if hc is None:
        tmux_bin = shutil.which("tmux")
        if not tmux_bin:
            die("'tmux' not found in PATH")
        os.execv(tmux_bin, [tmux_bin, "attach", "-d", "-t", name])
    print(f"luv: attaching {name} on {hc['host']}")
    exec_ssh(hc, shlex.join(["tmux", "attach", "-d", "-t", name]))


def remote_prompt(args: list[str]) -> str | None:
    """The prompt text, for labelling the session in the registry.

    Mirrors how main() derives the prompt for each command form.
    """
    if args[0] == "-l":
        return " ".join(args[2:]) or None
    if "-pr" in args:
        idx = args.index("-pr")
        return " ".join(a for i, a in enumerate(args)
                        if i not in (0, idx, idx + 1)) or None
    if len(args) > 1 and args[1].isdigit():
        return " ".join(args[2:]) or None
    return " ".join(args[1:]) or None


def dispatch_remote(hc: dict, remote_args: list[str], *, workspace: str | None = None,
                    use_tmux: bool = True, tty: bool = True,
                    meta: dict | None = None, extra_env: dict[str, str] | None = None) -> None:
    """Re-invoke luv on the remote host, inside tmux. Replaces this process.

    tmux wraps the *whole* remote invocation, not just the agent, so the clone
    and any docker compose start-up are inside the pane from second zero and a
    dropped connection never loses work.
    """
    sid = new_session_id()
    env = {"_LUV_INNER": "1", "_LUV_ID": sid}
    if hc.get("dir"):
        env["_LUV_PRS_DIR"] = str(hc["dir"])
    # Forward LUV_* with the prefix intact; the remote luv does its own stripping.
    for key, value in (extra_env or {}).items():
        env[key] = value

    session = None
    if use_tmux:
        if workspace:
            session = tmux_session_name(workspace)
        else:
            # The workspace number comes from gh api on the remote, so the name
            # isn't knowable here; the remote renames this once it knows.
            session = f"luv-pending-{sid}"
            env["_LUV_TMUX_PENDING"] = session

    inner = ["env"] + [f"{k}={v}" for k, v in env.items()]
    inner += [str(hc.get("luv_bin") or "luv")] + remote_args
    cmd = shlex.join(["tmux", "new-session", "-A", "-s", session, "--"] + inner
                     if use_tmux else inner)

    if meta is not None:
        record_session({**meta, "id": sid, "host": hc["host"], "session": session,
                        "workspace": workspace, "created": int(time.time())})

    print(f"luv: {hc['host']} — {session or 'no tmux'}")
    exec_ssh(hc, cmd, tty=tty)


def relative_age(ts: int | None) -> str:
    """Compact '2m ago' style age for the session table."""
    if not ts:
        return "-"
    delta = max(0, int(time.time()) - int(ts))
    for size, unit in ((86400, "d"), (3600, "h"), (60, "m")):
        if delta >= size:
            return f"{delta // size}{unit} ago"
    return f"{delta}s ago"


PR_TTL_OK = 300      # a PR we already found changes slowly
PR_TTL_MISS = 60     # no PR yet — the agent may open one any minute
PR_TIMEOUT = 10      # never let a wedged gh hang `luv ls`
PR_KEYS = ("pr_number", "pr_url", "pr_checked")  # cached in sessions.json


def fetch_pr(org: str, repo: str, number: int) -> dict | None:
    """The PR for workspace {repo}-{number}, found by its luv-{number} branch.

    Deliberately a head query and nothing else: asking whether PR #number exists
    would happily return a stranger's PR whenever someone took that number
    between luv reserving the folder and the agent pushing. Sessions opened from
    an existing PR carry pr_hint instead — see attach_pr_links.
    """
    r = run(["gh", "api", f"repos/{org}/{repo}/pulls",
             "-f", "state=all", "-f", f"head={org}:luv-{number}",
             "-f", "per_page=1", "-f", "sort=created", "-f", "direction=desc"],
            timeout=PR_TIMEOUT)
    if r.returncode != 0:
        return None
    try:
        prs = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(prs, list) or not prs:
        return None
    pr = prs[0]
    if not pr.get("html_url"):
        return None
    return {"number": pr.get("number"), "url": pr["html_url"]}


def attach_pr_links(rows: list[dict]) -> bool:
    """Fill in pr_number/pr_url on each session, re-asking GitHub past the TTL.

    Mutates the entries in place and reports whether anything changed, so the
    caller only pays for a write when there is something new. The cache is what
    keeps repeat `luv ls` runs instant and keeps a link on screen when GitHub
    (or the network) is unavailable.
    """
    now = int(time.time())
    changed = False
    stale = []
    for s in rows:
        org, repo = s.get("org"), s.get("repo")
        # Opened from a known PR (-l / -pr): the number is already right and its
        # head ref isn't luv-N, so resolve it without touching the network.
        hint = s.get("pr_hint")
        if hint and org and repo:
            url = f"https://github.com/{org}/{repo}/pull/{hint}"
            changed = changed or s.get("pr_url") != url
            s["pr_number"], s["pr_url"], s["pr_checked"] = hint, url, now
            continue
        number = workspace_number(s.get("workspace"))
        if not (org and repo and number is not None):
            continue
        ttl = PR_TTL_OK if s.get("pr_url") else PR_TTL_MISS
        if now - int(s.get("pr_checked") or 0) < ttl:
            continue
        stale.append((s, org, repo, number))

    if not stale:
        return changed
    if not shutil.which("gh"):
        print("luv: warning: 'gh' not found — PR column shows last known state",
              file=sys.stderr)
        return changed

    def probe(item: tuple) -> dict | None:
        _, org, repo, number = item
        return fetch_pr(org, repo, number)

    if len(stale) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(8, len(stale))) as pool:
            found = list(pool.map(probe, stale))
    else:
        found = [probe(stale[0])]

    for (s, *_), pr in zip(stale, found):
        s["pr_number"] = pr["number"] if pr else None
        s["pr_url"] = pr["url"] if pr else None
        s["pr_checked"] = now
    return True


def hyperlink(url: str | None, label: str) -> str:
    """OSC 8 hyperlink, so a short '#37' is still clickable.

    Off a terminal the escapes would be noise and the number alone useless, so
    piped output gets the bare URL instead.
    """
    if not url:
        return label
    if not sys.stdout.isatty():
        return url
    return f"\033]8;;{url}\033\\{label}\033]8;;\033\\"


def refresh_sessions(identity: str | None = None) -> tuple[list[dict], set[str]]:
    """Reconcile and persist the registry, warning about unreachable hosts."""
    with session_lock():
        sessions, unreachable = reconcile(load_sessions(), identity)
        save_sessions(sessions)
    for host in sorted(unreachable):
        print(f"luv: warning: {host or 'local'} unreachable — showing last known state",
              file=sys.stderr)
    return sessions, unreachable


def session_sort_key(s: dict) -> int:
    return int(s.get("activity") or s.get("last_seen") or s.get("created") or 0)


def print_sessions(rows: list[dict]) -> None:
    """Print the session table, truncating the prompt to the terminal width."""
    headers = ("HOST", "SESSION", "WORKSPACE", "AGENT", "ATTACHED", "ACTIVE",
               "PR", "PROMPT")
    last = len(headers) - 1  # PROMPT is the elastic column: truncated, not padded
    pr_col = last - 1
    table, links = [], []
    for s in rows:
        url = s.get("pr_url")
        # A terminal can hyperlink, so '#37' says it in four columns; piped
        # output has nothing to click and gets the URL itself.
        pr = f"#{s['pr_number']}" if url and s.get("pr_number") and sys.stdout.isatty() \
            else (url or "-")
        links.append(url)
        table.append((
            s.get("host") or "local",
            s.get("session") or "-",
            s.get("workspace") or "-",
            s.get("agent") or "-",
            "?" if s.get("live") is None else ("yes" if s.get("attached") else "no"),
            relative_age(session_sort_key(s)),
            pr,
            (s.get("prompt") or "-").replace("\n", " "),
        ))
    widths = [max(len(headers[i]), max(len(r[i]) for r in table)) for i in range(last)]
    used = sum(widths) + 2 * len(widths)
    room = max(12, shutil.get_terminal_size((100, 24)).columns - used)
    print("  ".join(headers[i].ljust(widths[i]) for i in range(last))
          + "  " + headers[last])
    for row, url in zip(table, links):
        cells = [row[i].ljust(widths[i]) for i in range(last)]
        # Pad outside the escape so the link covers '#37', not trailing spaces.
        cells[pr_col] = (hyperlink(url, row[pr_col])
                         + " " * (widths[pr_col] - len(row[pr_col])))
        prompt = row[last] if len(row[last]) <= room else row[last][:room - 1] + "…"
        print("  ".join(cells) + "  " + prompt)


def cmd_ls(args: list[str], identity: str | None = None) -> None:
    """List luv sessions across all hosts, reconciled against live tmux state."""
    host_filter = None
    if "--host" in args:
        idx = args.index("--host")
        if idx + 1 >= len(args):
            die("--host requires a host name")
        host_filter = args[idx + 1]

    sessions, unreachable = refresh_sessions(identity)

    if "--prune" in args:
        # Reconcile already dropped dead sessions; this also forgets hosts that
        # are gone for good rather than merely offline.
        keep = [s for s in sessions if (s.get("host") or "") not in unreachable]
        dropped = len(sessions) - len(keep)
        with session_lock():
            save_sessions(keep)
        print(f"luv: pruned {dropped} entr{'y' if dropped == 1 else 'ies'}, "
              f"{len(keep)} remaining")
        return

    rows = [s for s in sessions if not host_filter or s.get("host") == host_filter]
    if not rows:
        print("luv: no sessions")
        return
    if "--no-pr" not in args and attach_pr_links(rows):
        # Re-read under the lock rather than writing back the list we already
        # have: a concurrent dispatch may have appended an entry since
        # refresh_sessions released it. Only the pr_* keys are ours to merge.
        cached = {s["id"]: {k: s.get(k) for k in PR_KEYS} for s in rows if s.get("id")}
        with session_lock():
            stored = load_sessions()
            for s in stored:
                if s.get("id") in cached:
                    s.update(cached[s["id"]])
            save_sessions(stored)
    rows.sort(key=session_sort_key, reverse=True)
    print_sessions(rows)


def cmd_continue(args: list[str], identity: str | None = None) -> None:
    """Attach to a live luv session; picks or prompts when ambiguous."""
    if args and args[0] == "--list":
        cmd_ls([], identity)
        return

    sessions, _ = refresh_sessions(identity)
    live = [s for s in sessions if s.get("live")]

    label = ""
    if args:
        repo = args[0].rstrip("/").rsplit("/", 1)[-1]
        label = f" for '{repo}'"
        live = [s for s in live if s.get("repo") == repo]
        if len(args) > 1 and args[1].isdigit():
            want = f"{repo}-{int(args[1])}"
            label = f" for '{want}'"
            live = [s for s in live if s.get("workspace") == want]

    if not live:
        die(f"no live luv sessions{label}")
    live.sort(key=session_sort_key, reverse=True)

    if len(live) == 1 or args:
        target = live[0]  # an explicit repo means "the newest one for it"
    else:
        print(f"luv: {len(live)} live sessions:")
        print_sessions(live)
        print()
        for i, s in enumerate(live, 1):
            print(f"  {i}) {s.get('session')}  ({s.get('host') or 'local'})")
        raw = input("Choice [1]: ").strip() or "1"
        try:
            idx = int(raw)
        except ValueError:
            die(f"invalid choice: '{raw}'")
        if not 1 <= idx <= len(live):
            die(f"invalid choice: {idx}")
        target = live[idx - 1]

    host = target.get("host")
    attach_session(resolve_host(host, identity) if host else None, target["session"])


def main() -> None:
    args = sys.argv[1:]

    nav_mode = "-n" in args
    resume_mode = "-r" in args
    plan_mode = "-p" in args
    non_interactive = "-nit" in args
    force = "-f" in args or "--force" in args
    safe = "--safe" in args
    env_mode = "-e" in args
    local_mode = "--local" in args

    if "--claude" in args and "--codex" in args:
        die("--claude and --codex are mutually exclusive")
    agent = "codex" if "--codex" in args else "claude"
    if agent == "codex" and plan_mode:
        die("-p is only supported with Claude")

    # Value flags must be extracted before the boolean-flag strip below.
    def take_value(flag: str, what: str) -> str | None:
        nonlocal args
        if args.count(flag) > 1:
            die(f"{flag} may only be provided once")
        if flag not in args:
            return None
        idx = args.index(flag)
        if idx + 1 >= len(args):
            die(f"{flag} requires {what}")
        value = args[idx + 1].strip()
        if not value or value.startswith("-"):
            die(f"{flag} requires {what}")
        args = args[:idx] + args[idx + 2:]
        return value

    model = take_value("-m", "a model name")
    base = take_value("-b", "a branch name")
    host_flag = take_value("-s", "a host name")
    identity = take_value("-i", "a path to an SSH key")

    if local_mode and (host_flag or identity):
        die("--local cannot be combined with -s or -i")

    args = [a for a in args if a not in ("-n", "-r", "-e", "-f", "--force", "-p", "-nit", "--safe", "--claude", "--codex", "--local")]
    extra_env = collect_luv_env() if env_mode else {}

    # _LUV_INNER marks the remote-side luv: it must never dispatch onward.
    host_cfg = (None if local_mode or os.environ.get("_LUV_INNER")
                else resolve_host(host_flag, identity))

    if not args or args[0] in ("-h", "--help"):
        print("""\
Usage: luv [flags] <command>

Flags:
  --claude      launch Claude Code (default)
  --codex       launch Codex in YOLO mode (no approvals or sandbox)
  -n            navigate: open a shell instead of launching an agent
  -r            resume: resume the selected agent's last session
  -p            launch Claude in plan permission mode (default: bypassPermissions)
  -nit          non-interactive: run the selected agent and exit (no REPL)
  -m MODEL      model to use (Claude default: claude-opus-5; Codex: CLI default)
  -b BRANCH     base a new workspace off BRANCH (clone + branch from it; recorded in git config luv.base)
  -e            env: pass LUV_* environment variables (with prefix stripped) into the session
  -s HOST       run on HOST over SSH (overrides the configured remote host)
  -i PATH       SSH identity file to use for this invocation
  --local       force local execution even when a remote host is configured
  -f, --force   (with --clean) skip safety checks and delete all work folders
  --safe        (with --clean -f) only delete folders older than 24h

Commands:
  luv config                              interactive setup (remote host, SSH key, org)
  luv config set|get|unset <key> [value]  read or write a single setting
  luv config list                         show all settings
  luv --init                              configure default GitHub org only
  luv ls [--host H] [--prune] [--no-pr]   list live sessions across hosts
  luv continue [<repo> [number]]          attach to a live session
  luv [org/]<repo> [prompt...]            create a new PR workspace
  luv [org/]<repo> -b <branch> [prompt]   create a workspace based off <branch>
  luv [org/]<repo> <number> [prompt]      reopen an existing work folder by number
  luv -l <PR URL> [prompt]                open any GitHub PR by URL
  luv [org/]<repo> -pr <number> [prompt]  open a GitHub PR by repo + number
  luv [org/]<repo> -n                     open shell in latest local clone
  luv [org/]<repo> -r                     resume the selected agent in latest local clone
  luv --clean [-f] [--safe]               delete fully-pushed work folders

Org resolution:
  Explicit org/repo overrides the default. Run 'luv --init' to set a default.
  Config: ~/.luv/config.json

Remote:
  Once 'luv config' has a remote host, every workspace command runs there inside
  a tmux session that survives disconnects. 'luv ls' shows what is running and
  'luv continue' reattaches. Use --local for a one-off local run.
  Requires luv, tmux, gh and git on the remote. See docs/remote-sessions.md.

Docker:
  If the repo contains .luv/settings.json with a "compose_file" key,
  luv starts a Docker Compose environment and runs the selected agent inside the
  "dev-environment" service. Torn down automatically on exit.""")
        sys.exit(0)

    # Local-only commands: the config and the session registry live on this
    # machine, so these never dispatch to a remote host.
    if args[0] == "config":
        cmd_config(args[1:])
        return

    if args[0] == "--init":
        cmd_init()
        return

    if args[0] == "ls":
        cmd_ls(args[1:], identity)
        return

    if args[0] == "continue":
        cmd_continue(args[1:], identity)
        return

    if safe and (args[0] != "--clean" or not force):
        die("--safe only works with --clean -f")

    # -b only makes sense when creating a NEW workspace; reject it on every path
    # that early-returns without cloning+branching from scratch.
    if base is not None:
        is_reopen_by_number = len(args) > 1 and args[1].isdigit()
        opens_latest_local = (nav_mode or resume_mode) and len(args) == 1
        if (args[0] in ("--clean", "--init", "-l")
                or "-pr" in args
                or is_reopen_by_number
                or opens_latest_local):
            die("-b only applies when creating a new workspace "
                "(luv [org/]<repo> [prompt...]); it cannot be combined with "
                "--clean, --init, -l, -pr, reopen-by-number, or bare -n/-r")

    # Everything below this point does real workspace work, so it is what gets
    # handed to the remote machine when a host is configured.
    if host_cfg is not None:
        remote_args = list(args)
        org_hint = repo_hint = workspace = None

        if not remote_args[0].startswith("-"):
            raw = remote_args[0].rstrip("/")
            explicit, repo_hint = raw.split("/", 1) if "/" in raw else (None, raw)
            # Resolve the org here so the remote never needs its own default.
            org_hint = resolve_org(explicit)
            remote_args[0] = f"{org_hint}/{repo_hint}"

        # The workspace folder is {repo}-{number}, so for these forms the tmux
        # session name is knowable now and 'new-session -A' doubles as attach.
        # -l and -pr also pin down the PR itself, which `luv ls` can't otherwise
        # find: their branch is the PR's head ref, not luv-{number}.
        pr_hint = None
        if args[0] == "-l" and len(args) > 1:
            m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", args[1])
            if m:
                org_hint, repo_hint = m.group(1), m.group(2)
                pr_hint = int(m.group(3))
                workspace = f"{repo_hint}-{pr_hint}"
        elif repo_hint and "-pr" in args:
            idx = args.index("-pr")
            if idx + 1 < len(args) and args[idx + 1].isdigit():
                pr_hint = int(args[idx + 1])
                workspace = f"{repo_hint}-{pr_hint}"
        elif repo_hint and len(args) > 1 and args[1].isdigit():
            workspace = f"{repo_hint}-{int(args[1])}"

        for flag, enabled in (("--codex", agent == "codex"), ("-n", nav_mode),
                              ("-r", resume_mode), ("-p", plan_mode),
                              ("-nit", non_interactive), ("-e", env_mode),
                              ("-f", force), ("--safe", safe)):
            if enabled:
                remote_args.append(flag)
        if model:
            remote_args += ["-m", model]
        if base:
            remote_args += ["-b", base]

        # -nit streams stream-json to a local consumer and --clean just prints;
        # neither wants a tmux session or a registry entry.
        use_tmux = not (non_interactive or args[0] == "--clean")
        prompt_text = remote_prompt(args)
        meta = None
        if use_tmux:
            meta = {"org": org_hint, "repo": repo_hint, "agent": agent,
                    "prompt": prompt_text, "pr_hint": pr_hint}
            if workspace and prompt_text:
                print(f"luv: note: if {tmux_session_name(workspace)} is already "
                      "running, luv attaches to it and this prompt is not sent",
                      file=sys.stderr)

        dispatch_remote(host_cfg, remote_args, workspace=workspace,
                        use_tmux=use_tmux, tty=not non_interactive, meta=meta,
                        extra_env={k: v for k, v in os.environ.items()
                                   if env_mode and k.startswith("LUV_") and len(k) > 4})
        return  # unreachable

    if args[0] == "--clean":
        cmd_clean(force=force, safe=safe)
        return

    # luv -l <PR URL>
    if args[0] == "-l":
        if len(args) < 2:
            die("usage: luv -l <PR URL>")
        url = args[1]
        m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", url)
        if not m:
            die(f"cannot parse PR URL: {url}")
        org, repo, number = m.group(1), m.group(2), int(m.group(3))
        prompt = " ".join(args[2:]) or None
        open_pr(org, repo, number, prompt, nav_mode, resume_mode, plan_mode, non_interactive, extra_env=extra_env, model=model, agent=agent)
        return

    raw = args[0].rstrip("/")
    if "/" in raw:
        explicit_org, repo = raw.split("/", 1)
    else:
        explicit_org, repo = None, raw

    # luv [org/]<repo> -pr <number>
    if "-pr" in args:
        idx = args.index("-pr")
        if idx + 1 >= len(args):
            die("usage: luv <repo> -pr <number>")
        try:
            number = int(args[idx + 1])
        except ValueError:
            die(f"expected a PR number after -pr, got '{args[idx + 1]}'")
        prompt_parts = [a for i, a in enumerate(args) if i not in (0, idx, idx + 1)]
        prompt = " ".join(prompt_parts) or None
        open_pr(resolve_org(explicit_org), repo, number, prompt, nav_mode, resume_mode, plan_mode, non_interactive, extra_env=extra_env, model=model, agent=agent)
        return

    # Detect optional numeric second argument
    if len(args) > 1 and args[1].isdigit():
        number = int(args[1])
        prompt = " ".join(args[2:]) or None
        open_existing(resolve_org(explicit_org), repo, number, prompt, nav_mode, resume_mode, plan_mode, non_interactive, extra_env=extra_env, model=model, agent=agent)
        return

    org = resolve_org(explicit_org)
    prompt = " ".join(args[1:]) if len(args) > 1 else None

    # luv <repo> -n/-r  →  open latest local clone (no new workspace)
    if (nav_mode or resume_mode) and not prompt:
        clone_dir = find_latest_clone(repo)
        if clone_dir is None:
            die(f"no local clones of '{repo}' found in {PRS_DIR}")
        print(f"luv: opening latest clone {clone_dir.name}")
        if nav_mode:
            navigate(clone_dir, extra_env=extra_env)
        else:
            ensure_pr_rules(agent)
            resume(clone_dir, extra_env=extra_env, model=model, agent=agent)
        return

    # 1. Verify repo exists
    r = run(["gh", "api", f"repos/{org}/{repo}"])
    if r.returncode != 0:
        die(f"repo '{org}/{repo}' not found or gh auth failed.\n{r.stderr.strip()}")

    clone_url = f"https://github.com/{org}/{repo}"

    # 1b. If a base branch was requested, verify it exists before cloning.
    if base is not None:
        r = run(["git", "ls-remote", "--heads", clone_url, base])
        if r.returncode != 0 or f"refs/heads/{base}" not in r.stdout:
            die(f"base branch '{base}' not found on {org}/{repo}")

    # 2. Get latest issue/PR number (shared counter on GitHub).
    # /issues is documented to include PRs but in practice returns [] for repos
    # with no plain issues, so query both endpoints and take the max.
    def _latest(endpoint: str) -> int:
        r = run(["gh", "api",
                 f"repos/{org}/{repo}/{endpoint}?state=all&per_page=1&sort=created&direction=desc"])
        if r.returncode != 0:
            die(f"failed to fetch {endpoint}.\n{r.stderr.strip()}")
        items = json.loads(r.stdout)
        return items[0]["number"] if items else 0

    latest = max(_latest("issues"), _latest("pulls"))
    candidate = latest + 1

    # 3. Find free local folder
    PRS_DIR.mkdir(parents=True, exist_ok=True)
    while (PRS_DIR / f"{repo}-{candidate}").exists():
        candidate += 1
    clone_dir = PRS_DIR / f"{repo}-{candidate}"

    # 4. Clone (off the base branch when -b was given)
    print(f"luv: cloning {clone_url} -> {clone_dir}" + (f" (base {base})" if base else ""))
    clone_cmd = ["git", "clone"]
    if base is not None:
        clone_cmd += ["--branch", base]
    clone_cmd += [clone_url, str(clone_dir)]
    r = subprocess.run(clone_cmd)
    if r.returncode != 0:
        die(f"git clone failed (exit {r.returncode})")

    # 5. Create branch off the cloned HEAD (= base when -b was given, else default)
    branch = f"luv-{candidate}"
    print(f"luv: creating branch {branch}")
    r = subprocess.run(["git", "checkout", "-b", branch], cwd=str(clone_dir))
    if r.returncode != 0:
        die(f"git checkout -b failed (exit {r.returncode})")

    # 5b. Record the base so the eventual PR can target it (local .git/config only).
    if base is not None:
        r = run(["git", "config", "luv.base", base], cwd=str(clone_dir))
        if r.returncode != 0:
            print(f"luv: warning: could not record base branch ({r.stderr.strip()})",
                  file=sys.stderr)

    # 6. Ensure PR rules in ~/.claude/CLAUDE.md and bypass-permissions default
    ensure_pr_rules(agent)
    if agent == "claude":
        ensure_default_permission_mode()

    print(f"luv: ready — {clone_dir.name}, branch {branch}")

    # 7. Launch claude, resume session, or open shell (replace this process)
    if nav_mode:
        navigate(clone_dir, extra_env=extra_env)
    elif resume_mode:
        resume(clone_dir, extra_env=extra_env, model=model, agent=agent)
    else:
        launch(clone_dir, prompt, plan_mode=plan_mode, non_interactive=non_interactive, extra_env=extra_env, model=model, agent=agent)
