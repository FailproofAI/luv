import contextlib
import hashlib
import json
import os
import random
import re
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

try:
    import termios
except ImportError:  # not POSIX — the ssh/tmux handoffs don't run there either
    termios = None

LUV_DIR = Path.home() / ".luv"
CONFIG_FILE = LUV_DIR / "config.json"
SESSIONS_FILE = LUV_DIR / "sessions.json"
SESSIONS_LOCK = LUV_DIR / "sessions.lock"
TUNNEL_DIR = LUV_DIR / "tun"
PORTS_LOG = LUV_DIR / "ports.log"
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


def run(cmd: list[str], *, cwd: str | None = None, timeout: float | None = None,
        stdin: int | None = None) -> subprocess.CompletedProcess:
    """Capture a subprocess. A timeout surfaces as a failure, not an exception,
    so callers keep their plain returncode checks."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd,
                              timeout=timeout, stdin=stdin)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout}s")


def fan_out(fn, items: list) -> list:
    """Map fn over items concurrently, preserving order.

    A single item runs inline: every caller here fans out over hosts or GitHub
    queries, and a thread pool for one ssh is pure overhead.
    """
    if len(items) <= 1:
        return [fn(item) for item in items]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, len(items))) as pool:
        return list(pool.map(fn, items))


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


def ssh_base(hc: dict, *, tty: bool = False, batch: bool = False,
             control: str | None = None, master: bool = False,
             control_op: list[str] | None = None) -> list[str]:
    """Build the ssh argv prefix for a host. Every SSH call site goes through
    here — an identity file that applied to some commands but not others would
    be worse than none at all.

    `control` points at a multiplexing socket, which is how the port forwarder
    adds and drops tunnels on a connection that is already up. It is only ever
    passed by the forwarder: sharing one connection with the interactive session
    would mean a dying tunnel could take an agent's terminal down with it.
    """
    cmd = ["ssh"]
    if tty:
        cmd.append("-t")
    if batch:
        cmd += ["-o", "BatchMode=yes", "-o", "ConnectTimeout=5"]
    if control:
        cmd += ["-S", str(control)]
        if master:
            # ControlPersist=yes rather than a timeout: forwards are meant to
            # outlive detaching, and an idle connection is what that looks like.
            cmd += ["-M", "-N", "-f", "-o", "ControlPersist=yes",
                    "-o", "ExitOnForwardFailure=no"]
        # Control commands (-O check/forward/cancel/exit) have to precede the
        # host, or ssh reads them as the command to run on the far end.
        cmd += [str(o) for o in (control_op or [])]
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


SLUG_MAX = 8


def sanitize_slug(raw: str) -> str:
    """Lowercase alphanumerics only, capped at SLUG_MAX.

    Dropping separators rather than replacing them is what keeps workspace_re
    unambiguous: with no '-' inside a slug, '{repo}-{slug}-{number}' has exactly
    one reading. 'gpu-box-01' collapses to 'gpubox01' rather than 'gpu', so two
    similarly-named boxes stay distinguishable.
    """
    return re.sub(r"[^a-z0-9]", "", raw.lower())[:SLUG_MAX]


def machine_slug() -> str:
    """Short token identifying this machine, for workspace and branch names.

    Workspace numbers come from GitHub's issue counter, which every machine
    computes independently — so two machines racing on the same repo both pick
    the same number and would push the same branch. The slug is what keeps them
    apart. Config wins over the hostname so the name can be something readable.
    """
    configured = load_config().get("machine")
    if isinstance(configured, str) and sanitize_slug(configured):
        return sanitize_slug(configured)
    return sanitize_slug(socket.gethostname().split(".")[0]) or "local"


def workspace_name(repo: str, number: int, slug: str | None = None,
                   copy: int = 1) -> str:
    """Folder name for a workspace: '{repo}-{machine}-{number}[_{copy}]'.

    The copy suffix exists for `luv -l`, which clones a PR into a folder of its
    own every time rather than reopening the one already there. '_' is the one
    separator that survives every name derived from this: tmux forbids '.' and
    ':' in session names, and Compose project names forbid '.' too.
    """
    base = f"{repo}-{slug or machine_slug()}-{number}"
    return base if copy <= 1 else f"{base}_{copy}"


def next_workspace_dir(repo: str, number: int) -> Path:
    """A workspace path for this number that nothing occupies yet.

    Second and later clones of the same PR land on '..._2', '..._3', and so on.
    """
    copy = 1
    while (PRS_DIR / workspace_name(repo, number, copy=copy)).exists():
        copy += 1
    return PRS_DIR / workspace_name(repo, number, copy=copy)


def branch_name(number: int, slug: str | None = None) -> str:
    """Branch luv creates for a new workspace: 'luv-{machine}-{number}'."""
    return f"luv-{slug or machine_slug()}-{number}"


def workspace_re(repo: str) -> re.Pattern:
    """Match this repo's workspace folders, slugged or legacy.

    The repo name is known at every call site, so it can be anchored — which is
    what makes the optional middle group unambiguous. Group 1 is the slug (None
    for a pre-slug folder), group 2 the number, group 3 the copy index (None for
    the first clone of that number).
    """
    return re.compile(rf"^{re.escape(repo)}-(?:([a-z0-9]+)-)?(\d+)(?:_(\d+))?$")


def branch_re(number: int) -> re.Pattern:
    """Match luv branches for a number, slugged or legacy."""
    return re.compile(rf"^luv-(?:[a-z0-9]+-)?{number}$")


def workspace_branch(repo: str, folder: str) -> str | None:
    """The branch a workspace folder was created with, or None if it isn't one.

    Read back off the folder rather than rebuilt from this machine's slug: the
    folder keeps the slug of whichever machine made it, both after a handover
    and for a session another machine started. A pre-slug folder means a
    pre-slug branch, so the legacy name is the right answer there.
    """
    m = workspace_re(repo).match(folder or "")
    if not m:
        return None
    slug, number = m.group(1), m.group(2)
    return f"luv-{slug}-{number}" if slug else f"luv-{number}"


def find_workspace(repo: str, number: int) -> Path | None:
    """Locate an existing workspace folder for {repo}-{number}, or None.

    Ours first, then one that arrived here by handover (it keeps the slug of the
    machine that created it), then a pre-slug folder. Where `luv -l` has cloned
    the same PR more than once, the newest copy wins — that is the one you just
    made. Two foreign *machines* is still genuinely ambiguous: the number alone
    cannot say which was meant.
    """
    if not PRS_DIR.exists():
        return None

    pattern = workspace_re(repo)
    mine = machine_slug()
    ours: list[tuple[int, Path]] = []
    foreign: dict[str, list[tuple[int, Path]]] = {}
    legacy = None
    for entry in sorted(PRS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        m = pattern.match(entry.name)
        if not m or int(m.group(2)) != number:
            continue
        slug, copy = m.group(1), int(m.group(3) or 1)
        if slug is None:
            legacy = entry
        elif slug == mine:
            ours.append((copy, entry))
        else:
            foreign.setdefault(slug, []).append((copy, entry))

    def newest(copies: list[tuple[int, Path]]) -> Path:
        return max(copies, key=lambda c: c[0])[1]

    if ours:
        return newest(ours)
    if len(foreign) > 1:
        names = ", ".join(sorted(e.name for c in foreign.values() for _, e in c))
        die(f"ambiguous workspace for '{repo}' {number}: {names}\n"
            f"       open one by name with 'luv {repo} -n' or remove the stale folder")
    if foreign:
        return newest(next(iter(foreign.values())))
    return legacy


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
        # A stamped @luv_workspace is proof enough on its own: a session luv
        # started inside a tmux you opened yourself keeps the name you gave it.
        if len(parts) != 5 or not (parts[1].startswith("luv-") or parts[2]):
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


def folder_number(name: str | None) -> int | None:
    """The trailing N of any workspace folder, whether or not it carries a slug.

    Repo-agnostic on purpose: the callers that need this — the `rm -rf` guard,
    the orphan scan, `--clean` — are looking at a directory listing and have no
    repo to anchor on. Where the repo *is* known, workspace_number reads the
    slug too and should be preferred.

    A '_{copy}' suffix is part of the number's segment, so it is stripped here
    too: '{repo}-{slug}-41_2' is still workspace 41.
    """
    if not name:
        return None
    parts = name.rsplit("-", 1)
    if len(parts) != 2:
        return None
    m = re.match(r"^(\d+)(?:_\d+)?$", parts[1])
    return int(m.group(1)) if m else None


def parse_github_url(url: str) -> tuple[str, str] | None:
    """(org, repo) from a GitHub clone URL, in either form. None if neither."""
    m = re.match(r"https://github\.com/([^/]+)/([^/.]+)", url.strip())
    if not m:
        m = re.match(r"git@github\.com:([^/]+)/([^/.]+)", url.strip())
    return (m.group(1), m.group(2)) if m else None


def remote_prs_dir(hc: dict | None) -> str:
    """The workspace root on a host, as one shell word for the remote side.

    Unconfigured hosts get $HOME/prs expanded there rather than here — the
    laptop's home directory is not the box's.
    """
    if hc is None:
        return shlex.quote(str(PRS_DIR))
    if hc.get("dir"):
        return shlex.quote(str(hc["dir"]))
    return '"$HOME/prs"'


def query_origins(hc: dict | None, workspaces: list[str]) -> dict[str, tuple[str, str]]:
    """(org, repo) per workspace folder, read from its git origin on the host.

    One round trip for the whole host. A session started from another machine
    arrives here as a folder name and nothing else, and the PR column needs an
    owner — which guessing the configured default would get wrong for every repo
    that isn't in it.
    """
    if not workspaces:
        return {}
    root = remote_prs_dir(hc)
    loop = ("for w in " + " ".join(shlex.quote(w) for w in workspaces) + "; do "
            f'echo "$w|$(git -C {root}/"$w" remote get-url origin 2>/dev/null)"; done')
    r = ssh_run(hc, loop)
    if r.returncode != 0:
        return {}
    found = {}
    for line in r.stdout.splitlines():
        name, _, url = line.partition("|")
        parsed = parse_github_url(url)
        if parsed:
            found[name] = parsed
    return found


def known_hosts(sessions: list[dict], host_filter: str | None = None) -> list[str]:
    """Hosts worth scanning: the registry's, every configured one, and local.

    Local stays in the set even when a remote is configured — `--local` runs and
    pre-remote workspaces still leave folders in this machine's PRS_DIR, and an
    empty registry must not mean "nowhere to look". Configured hosts are in it
    because sessions on them may have been started from a different machine,
    which leaves this registry with no entry to hang the host off.
    """
    hosts = {s.get("host") or "" for s in sessions} | {""}
    remote = load_config().get("remote")
    if isinstance(remote, dict):
        if remote.get("host"):
            hosts.add(remote["host"])
        overrides = remote.get("hosts")
        if isinstance(overrides, dict):
            hosts |= {h for h in overrides if isinstance(h, str) and h}
    if host_filter is not None:
        hosts &= {host_filter}
    return sorted(hosts)


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


def adopt(host: str, row: dict, now: int) -> dict:
    """A registry entry for a live session this machine never dispatched.

    Everything tmux knows and nothing beyond it: the prompt and the agent belong
    to whichever machine started the session, and a guess in those columns would
    be worse than a dash. The id is the session's own @luv_id when it has one,
    so both machines' registries agree on which session this is.

    The repo is left to attach_origins rather than read off the folder name:
    '{repo}-{machine}-{number}' cannot be split without already knowing one of
    the first two parts, and git on the host knows the answer for certain.
    """
    return {"id": row["id"] or new_session_id(), "host": host,
            "session": row["session"], "workspace": row["workspace"] or None,
            "adopted": True, "last_seen": now, "attached": row["attached"],
            "activity": row["activity"], "live": True}


def attach_origins(pending: dict[str, list[dict]], identity: str | None) -> None:
    """Fill in org/repo on entries that lack one, a round trip per host.

    Mutates in place. The answer goes into the registry, so a session pays for
    this once — unless the folder isn't there to ask yet, which is the one case
    where asking again next time is exactly right.
    """
    work = {h: [e["workspace"] for e in entries if e.get("workspace") and not e.get("org")]
            for h, entries in pending.items()}
    work = {h: ws for h, ws in work.items() if ws}
    if not work:
        return
    hosts = sorted(work)
    found = dict(zip(hosts, fan_out(
        lambda h: query_origins(resolve_host(h, identity) if h else None, work[h]),
        hosts)))
    for host in hosts:
        for entry in pending[host]:
            hit = found[host].get(entry.get("workspace"))
            if hit:
                entry["org"], entry["repo"] = hit


def reconcile(sessions: list[dict],
              identity: str | None = None) -> tuple[list[dict], set[str]]:
    """Refresh registry entries against live tmux state on every known host.

    Returns (entries, unreachable_hosts). Entries whose host did not answer are
    kept and flagged rather than pruned — running `luv ls` on a plane must not
    wipe the registry.

    Live sessions no entry claims are adopted rather than ignored. The registry
    only ever records what *this* machine dispatched, so without that a session
    started from your laptop is invisible from your desktop, even though both
    are looking at the same tmux server.
    """
    hosts = known_hosts(sessions)
    results = dict(zip(hosts, fan_out(
        lambda h: query_tmux(resolve_host(h, identity) if h else None), hosts)))

    now = int(time.time())
    kept: list[dict] = []
    claimed: set[tuple[str, str]] = set()
    # Entries whose workspace has no owner yet, per host. Adopted ones start out
    # that way; so does an entry adopted while its session was still called
    # luv-pending-<id>, whose folder only got a name later.
    unowned: dict[str, list[dict]] = {}
    for s in sessions:
        host = s.get("host") or ""
        live = results.get(host)
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
        claimed.add((host, match["session"]))
        if s.get("workspace") and not s.get("org"):
            unowned.setdefault(host, []).append(s)
        kept.append(s)

    for host in hosts:
        for row in results.get(host) or []:
            if (host, row["session"]) in claimed:
                continue
            claimed.add((host, row["session"]))
            entry = adopt(host, row, now)
            if entry["workspace"]:
                unowned.setdefault(host, []).append(entry)
            kept.append(entry)
    attach_origins(unowned, identity)

    return kept, {h for h, v in results.items() if v is None}


def parse_github_remote(cwd: str) -> tuple[str, str] | None:
    """Extract (org, repo) from origin remote URL. Returns None on failure."""
    r = run(["git", "remote", "get-url", "origin"], cwd=cwd)
    return parse_github_url(r.stdout) if r.returncode == 0 else None


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


# An rc file is a program, and some of them never finish: one that starts tmux,
# or waits on a prompt, would otherwise hang every session start.
SHELL_ENV_TIMEOUT = 15

# Vars that describe the shell we just ran rather than the setup we asked it
# about. Importing them would tell the agent it is somewhere it is not.
SHELL_ENV_SKIP = {"_", "OLDPWD", "PWD", "SHLVL"}


def shell_env() -> dict[str, str]:
    """Everything the user's login+interactive shell exports.

    tmux and ssh exec their command directly, with no shell in between, so a
    session luv started — a remote dispatch, a handover, a detached start —
    never sources ~/.bashrc or ~/.zshrc. It runs with whatever environment the
    tmux server was started with, frozen at whenever that server first came up.
    The API key an agent authenticates with and the PATH entry that makes
    'codex' resolvable both live in the rc, and both go missing.

    Asking $SHELL for its own environment is the only way to get them back: rc
    files are code, not data, and there is no reading them from outside. -l
    covers the profile files, -i the rc files; between them they reach where
    bash and zsh users actually put their exports.
    """
    shell = os.environ.get("SHELL") or "/bin/bash"
    marker = "__luv_env__"
    code = ("import json,os,sys;"
            f"sys.stdout.write({marker!r} + json.dumps(dict(os.environ)))")
    probe = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    # DEVNULL because an interactive shell sharing our tty would fight the
    # agent for it, and lose to SIGTTOU rather than to the timeout.
    r = run([shell, "-lic", probe], timeout=SHELL_ENV_TIMEOUT,
            stdin=subprocess.DEVNULL)
    # Match on the marker, not the exit status: rc files greet, warn, and fail
    # in ways that say nothing about whether the environment came back. What
    # precedes the marker is the greeting; raw_decode drops anything an exit
    # hook prints after it.
    if marker not in r.stdout:
        return {}
    try:
        env, _ = json.JSONDecoder().raw_decode(r.stdout.split(marker, 1)[1].lstrip())
    except ValueError:
        return {}
    return {k: v for k, v in env.items() if isinstance(v, str)}


def merged_path(current: str, from_rc: str) -> str:
    """`current` first, then whatever the rc adds. PATH order is a precedence
    list, so the binaries that got us this far keep winning."""
    entries = [p for p in current.split(os.pathsep) if p]
    entries += [p for p in from_rc.split(os.pathsep) if p and p not in entries]
    return os.pathsep.join(entries)


def apply_shell_env() -> None:
    """Fill this process's environment in from the user's shell.

    Only for the luv that tmux or ssh started (_LUV_INNER). A luv you ran
    yourself already inherited the shell it ran in; re-deriving it there would
    charge every session start for an rc that has already run.

    What is already set wins. 'FOO=bar luv …' and -e are statements about this
    session, while the rc is the background default, and a session that quietly
    overrode the value you handed it would be worse than one missing it. PATH
    is the exception, being a list rather than a value: the rc's entries are
    appended, which is how nvm's node and ~/.local/bin become findable.
    """
    if not os.environ.get("_LUV_INNER") or load_config().get("shell_env") is False:
        return
    for key, value in shell_env().items():
        if key in SHELL_ENV_SKIP:
            continue
        if key == "PATH":
            os.environ["PATH"] = merged_path(os.environ.get("PATH", ""), value)
        elif key not in os.environ:
            os.environ[key] = value


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

    # Asked first because it names every workspace and branch this machine
    # creates, and the hostname-derived default is rarely the nicest label.
    slug = input(f"This machine's name, used in workspace and branch names "
                 f"[{machine_slug()}]: ").strip()
    if slug:
        if not sanitize_slug(slug):
            die(f"'{slug}' has no letters or digits to make a machine name from")
        config["machine"] = sanitize_slug(slug)

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

        if folder_number(entry.name) is None:
            continue  # doesn't look like a luv workspace — skip silently

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

        cwd = str(entry)

        # Must be a git repo
        if run(["git", "rev-parse", "--git-dir"], cwd=cwd).returncode != 0:
            continue

        # Ask git for the branch rather than rebuilding it from the folder name:
        # the name carries the slug of the machine that created the workspace,
        # which is not this one after a handover.
        branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                     cwd=cwd).stdout.strip()
        if not branch or branch == "HEAD":
            skipped.append((entry.name, "detached HEAD"))
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
    """Return the highest-numbered local workspace for a repo, or None.

    Slugged and pre-slug folders compete on the number alone; a tie between our
    own slug and one that arrived by handover goes to ours, and a tie between
    two clones of the same PR goes to the newest copy.
    """
    if not PRS_DIR.exists():
        return None
    pattern = workspace_re(repo)
    mine = machine_slug()
    best: Path | None = None
    best_key = (-1, -1, -1)
    for entry in sorted(PRS_DIR.iterdir()):
        if not entry.is_dir():
            continue
        m = pattern.match(entry.name)
        if not m:
            continue
        key = (int(m.group(2)), 1 if m.group(1) == mine else 0,
               int(m.group(3) or 1))
        if key > best_key:
            best, best_key = entry, key
    return best


def open_existing(org: str, repo: str, number: int, prompt: str | None, nav_mode: bool = False, resume_mode: bool = False, plan_mode: bool = False, non_interactive: bool = False, extra_env: dict[str, str] | None = None, model: str | None = None, agent: str = "claude") -> None:
    """Open an existing work folder or remote branch by number."""
    extra_env = extra_env or {}
    clone_dir = find_workspace(repo, number)

    # 1. Local folder takes priority
    if clone_dir is not None:
        print(f"luv: opening existing folder {clone_dir.name}")
        ensure_pr_rules(agent)
        if nav_mode:
            navigate(clone_dir, extra_env=extra_env)
        elif resume_mode:
            resume(clone_dir, extra_env=extra_env, model=model, agent=agent)
        else:
            launch(clone_dir, prompt, plan_mode=plan_mode, non_interactive=non_interactive, extra_env=extra_env, model=model, agent=agent)
        return  # unreachable

    # 2. Check for a remote luv branch for this number.
    # The slug belongs to whichever machine created the workspace, so ours is
    # only the first guess — a branch pushed from another machine is still the
    # right one to check out here.
    clone_url = f"https://github.com/{org}/{repo}"
    r = run(["git", "ls-remote", "--heads", clone_url])
    pattern = branch_re(number)
    candidates = [line.split("refs/heads/", 1)[1]
                  for line in r.stdout.splitlines() if "refs/heads/" in line]
    candidates = [b for b in candidates if pattern.match(b)]
    if not candidates:
        die(f"no local folder for '{repo}' {number} "
            f"and no remote branch matching 'luv-*-{number}'")
    preferred = [branch_name(number), f"luv-{number}"]
    branch = next((b for b in preferred if b in candidates), candidates[0])
    if len(candidates) > 1:
        print(f"luv: warning: {len(candidates)} branches match "
              f"({', '.join(candidates)}); using {branch}", file=sys.stderr)

    # 3. Clone and checkout the existing branch
    clone_dir = PRS_DIR / workspace_name(repo, number)
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


def open_pr(org: str, repo: str, number: int, prompt: str | None, nav_mode: bool = False, resume_mode: bool = False, plan_mode: bool = False, non_interactive: bool = False, extra_env: dict[str, str] | None = None, model: str | None = None, agent: str = "claude", fresh: bool = False) -> None:
    """Open any GitHub PR by org/repo/number, cloning if needed.

    With fresh=True — how `luv -l` calls this — an existing folder for the same
    number is left alone and the PR is cloned again next to it. A URL you paste
    is a request to look at that PR as it is *now*, and a folder from last week
    sits on whatever was checked out then. -r is the exception: resuming means
    picking up a conversation, and a conversation lives in the folder it was
    held in.
    """
    extra_env = extra_env or {}
    clone_dir = find_workspace(repo, number)

    if clone_dir is not None and (resume_mode or not fresh):
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
    # A fork whose repo has since been deleted comes back as head.repo = null.
    head_repo = pr_data["head"].get("repo") or {}
    clone_url = head_repo.get("clone_url")
    if not clone_url:
        die(f"PR {org}/{repo}#{number} has no head repository — "
            "the fork it came from was deleted")
    base_url = (pr_data.get("base", {}).get("repo") or {}).get("clone_url")

    clone_dir = next_workspace_dir(repo, number)
    PRS_DIR.mkdir(parents=True, exist_ok=True)
    print(f"luv: cloning {clone_url} -> {clone_dir} (branch {branch})")
    r = subprocess.run(["git", "clone", clone_url, str(clone_dir)])
    if r.returncode != 0:
        die(f"git clone failed (exit {r.returncode})")
    r = subprocess.run(["git", "checkout", branch], cwd=str(clone_dir))
    if r.returncode != 0:
        die(f"git checkout {branch} failed (exit {r.returncode})")

    # A clone of the head repo has every branch that repo has — but for a PR
    # from a fork that is not where the base branch lives, so there would be
    # nothing to diff or rebase against. Fetch the base repo alongside it.
    if base_url and base_url != clone_url:
        print(f"luv: fetching base repo {base_url} as 'upstream'")
        for cmd in (["git", "remote", "add", "upstream", base_url],
                    ["git", "fetch", "upstream"]):
            r = subprocess.run(cmd, cwd=str(clone_dir))
            if r.returncode != 0:
                print(f"luv: warning: {' '.join(cmd[:3])} failed "
                      f"(exit {r.returncode}) — upstream branches unavailable",
                      file=sys.stderr)
                break

    print(f"luv: ready — {clone_dir.name}, branch {branch}")
    ensure_pr_rules(agent)
    if nav_mode:
        navigate(clone_dir, extra_env=extra_env)
    elif resume_mode:
        resume(clone_dir, extra_env=extra_env, model=model, agent=agent)
    else:
        launch(clone_dir, prompt, plan_mode=plan_mode, non_interactive=non_interactive, extra_env=extra_env, model=model, agent=agent)


# Terminal modes a full-screen program switches on and is expected to switch
# off again on its way out. A connection that dies mid-session never gets to,
# and the leftovers are user-visible: mouse tracking turns every mouse move
# into "35;22;1M" junk at the shell prompt, bracketed paste wraps pastes in
# "200~", and the alternate screen swallows the scrollback.
TERM_RESET = (
    "\x1b[?1000l\x1b[?1001l\x1b[?1002l\x1b[?1003l"   # mouse tracking off
    "\x1b[?1004l"                                    # focus reporting off
    "\x1b[?1005l\x1b[?1006l\x1b[?1015l\x1b[?1016l"   # mouse report encodings off
    "\x1b[?2004l"                                    # bracketed paste off
    "\x1b[?1049l"                                    # back to the primary screen
    "\x1b[?1l\x1b>"                                  # normal cursor keys, keypad
    "\x1b[r"                                         # full-height scroll region
    "\x1b[?7h\x1b[4l\x1b[?25h\x1b[0m"                # wrap, replace, cursor, colours
)


def terminal_fd() -> int | None:
    """The fd our terminal is on, or None when there isn't one.

    stdin first: it is the one a redirect is least likely to have taken away,
    and termios wants the terminal device rather than whichever stream happens
    to still point at it.
    """
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            fd = stream.fileno()
        except (AttributeError, ValueError, OSError):
            continue
        if os.isatty(fd):
            return fd
    return None


@contextlib.contextmanager
def terminal_guard():
    """Restore the terminal on the way out, however we get there.

    Two layers, because a killed program skips two different kinds of cleanup:
    termios settings (raw mode, echo) that ssh itself normally puts back, and
    the DEC private modes the *remote* program turned on, which nothing on this
    side knows about. Both are cheap no-ops when nothing was broken.
    """
    fd = terminal_fd()
    saved = None
    if fd is not None and termios is not None:
        with contextlib.suppress(termios.error, OSError):
            saved = termios.tcgetattr(fd)
    try:
        yield
    finally:
        if fd is not None:
            for stream in (sys.stdout, sys.stderr):
                with contextlib.suppress(ValueError, OSError):
                    stream.flush()
            if saved is not None:
                with contextlib.suppress(termios.error, OSError):
                    termios.tcsetattr(fd, termios.TCSADRAIN, saved)
            with contextlib.suppress(OSError):
                os.write(fd, TERM_RESET.encode())


def continue_hint(repo: str | None, workspace: str | None = None) -> str:
    """The 'luv continue …' line to hand back when a session breaks.

    As narrow as what we actually know, and no narrower: the number is read off
    the workspace folder, which a session dispatched before the remote picked
    one doesn't have yet. Every shorter form is still a command that works —
    a repo alone takes its newest session, and bare 'luv continue' asks.
    """
    parts = ["luv", "continue"]
    if repo:
        parts.append(repo)
        number = workspace_number(repo, workspace or "")
        if number is not None:
            parts.append(str(number))
    return " ".join(parts)


def reopen_hint(args: list[str]) -> str:
    """The 'luv <repo> [n] -r' line for a session whose tmux is already gone.

    Takes the same [<repo> [number]] grammar 'luv continue' does, so whatever
    was just typed carries straight over to the command that still works.
    """
    repo = args[0].rstrip("/").rsplit("/", 1)[-1]
    number = args[1] if len(args) > 1 and args[1].isdigit() else None
    return " ".join(["luv", repo] + ([number] if number else []) + ["-r"])


def hand_over(argv: list[str], *, restore: bool = True, watch=None,
              hint: str | None = None) -> None:
    """Give the terminal to a child and exit with its status. Never returns.

    Without `restore` this is a plain execv, which is the better deal when
    there is no terminal to leave in a bad state: no process in the middle, and
    TTY handling, Ctrl-C and the exit code all pass through for free.

    With it, we stay alive as a parent whose only job is to clean up. That
    costs one process and buys the case this exists for — ssh dying on a broken
    pipe, taking a remote tmux and its agent TUI with it, with nobody left to
    turn mouse tracking back off. The child is still in our foreground process
    group, so it keeps receiving Ctrl-C and SIGWINCH from the terminal driver
    exactly as it did under execv.

    `watch` gets to piggyback on that parent: it is the only process alive for
    the whole session, which is what the port forwarder needs to notice a server
    the agent starts ten minutes in. It runs on a daemon thread and touches
    neither the terminal nor termios — this path is the one that has to stay
    boring.

    `hint` is the way back in, printed when the child exits badly. A clean exit
    is either a detach or the agent finishing, and neither wants advice; a bad
    one is usually ssh losing the connection out from under a session that is
    still running on the other side, and the terminal it lands back in should
    not have to be told twice how to get there.
    """
    if not restore:
        os.execv(argv[0], argv)
    else:
        with terminal_guard():
            proc = subprocess.Popen(argv)
            if watch is not None:
                start_port_watcher(watch)
            while True:
                try:
                    code = proc.wait()
                    break
                except KeyboardInterrupt:
                    # The child got this same Ctrl-C from the tty and decides
                    # for itself what to do with it; outliving it is the point.
                    continue
        if code != 0 and hint:
            # After the guard, not inside it: a terminal still in the remote
            # program's modes is no place to print something to be copied.
            print(f"\nluv: session ended unexpectedly — continue it with:\n\n"
                  f"    {hint}\n", file=sys.stderr)
        sys.exit(code)


def exec_ssh(hc: dict, remote_cmd: str, *, tty: bool = True, watch=None,
             hint: str | None = None) -> None:
    """Hand the terminal to ssh. Never returns."""
    ssh_bin = shutil.which("ssh")
    if not ssh_bin:
        die("'ssh' not found in PATH")
    argv = ssh_base(hc, tty=tty) + [remote_shell(remote_cmd)]
    hand_over([ssh_bin] + argv[1:], restore=tty, watch=watch, hint=hint)


def attach_session(hc: dict | None, name: str, *, hint: str | None = None) -> None:
    """Attach to a tmux session, locally or over ssh. Never returns.

    -d detaches other clients so the pane isn't size-clamped to a stale window
    left open elsewhere; these are all the same user's sessions.
    """
    if hc is None:
        tmux_bin = shutil.which("tmux")
        if not tmux_bin:
            die("'tmux' not found in PATH")
        hand_over([tmux_bin, "attach", "-d", "-t", name], hint=hint)
        return
    print(f"luv: attaching {name} on {hc['host']}")
    exec_ssh(hc, shlex.join(["tmux", "attach", "-d", "-t", name]),
             watch=port_watch(hc, session=name), hint=hint)


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


def registry_workspace(host: str, repo: str, number: int) -> str | None:
    """Workspace folder recorded for this host/repo/number, if luv knows it."""
    pattern = workspace_re(repo)
    for s in load_sessions():
        if (s.get("host") or "") != host or s.get("repo") != repo:
            continue
        name = s.get("workspace")
        m = pattern.match(name) if isinstance(name, str) else None
        if m and int(m.group(2)) == number:
            return name
    return None


def resolve_remote_workspace(hc: dict, org: str | None, repo: str,
                             number: int) -> str | None:
    """Folder name the remote uses for {repo}-{number}, or None if unknowable.

    The slug in a workspace name belongs to the machine that created it, so the
    dispatcher can't compute this — it has to look it up. The registry answers
    for anything luv has launched; otherwise one cheap ssh asks the host itself.
    None leaves the caller on the luv-pending path, which renames on arrival.
    """
    known = registry_workspace(hc["host"], repo, number)
    if known:
        return known

    env = {"_LUV_INNER": "1"}
    if hc.get("dir"):
        env["_LUV_PRS_DIR"] = str(hc["dir"])
    cmd = shlex.join(["env"] + [f"{k}={v}" for k, v in env.items()]
                     + [str(hc.get("luv_bin") or "luv"), "--where",
                        f"{org}/{repo}" if org else repo, str(number)])
    r = ssh_run(hc, cmd)
    name = r.stdout.strip().splitlines()[-1] if r.stdout.strip() else ""
    if r.returncode != 0 or not workspace_re(repo).match(name):
        return None
    return name


def cmd_where(args: list[str]) -> None:
    """Print the workspace folder name for [org/]<repo> <number>.

    Exists for the dispatcher rather than for people: asking the machine that
    holds a workspace is the only way to learn which machine's slug it carries.
    """
    if len(args) < 2 or not args[1].isdigit():
        die("usage: luv --where [org/]<repo> <number>")
    repo = args[0].rstrip("/").rsplit("/", 1)[-1]
    number = int(args[1])
    found = find_workspace(repo, number)
    print(found.name if found else workspace_name(repo, number))


def dispatch_remote(hc: dict, remote_args: list[str], *, workspace: str | None = None,
                    use_tmux: bool = True, tty: bool = True, detach: bool = False,
                    meta: dict | None = None, extra_env: dict[str, str] | None = None) -> None:
    """Re-invoke luv on the remote host, inside tmux. Replaces this process.

    tmux wraps the *whole* remote invocation, not just the agent, so the clone
    and any docker compose start-up are inside the pane from second zero and a
    dropped connection never loses work.

    With detach=True the session is started in the background and this returns
    instead of handing over the terminal.
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
    tmux_args = ["tmux", "new-session"] + (["-d"] if detach else []) + \
                ["-A", "-s", session, "--"]
    cmd = shlex.join(tmux_args + inner if use_tmux else inner)

    if meta is not None:
        record_session({**meta, "id": sid, "host": hc["host"], "session": session,
                        "workspace": workspace, "created": int(time.time())})

    # Only a tmux session is there to come back to; -nit and --clean run to
    # completion over ssh and have nothing to continue.
    hint = continue_hint((meta or {}).get("repo"), workspace) if use_tmux else None
    print(f"luv: {hc['host']} — {session or 'no tmux'}")
    if detach:
        r = run(ssh_base(hc, batch=True) + [remote_shell(cmd)])
        if r.returncode != 0:
            die(f"could not start {session} on {hc['host']}: {r.stderr.strip()}")
        print(f"luv: started detached — attach with: {hint or 'luv continue'}")
        return
    exec_ssh(hc, cmd, tty=tty, watch=port_watch(hc, sid=sid, session=session),
             hint=hint)


def cmd_paths() -> None:
    """Print this machine's $HOME and workspace root, one per line.

    For the dispatcher, like --where: handover needs both as absolute paths on
    both machines, and only a machine can resolve its own config.
    """
    print(Path.home())
    print(PRS_DIR)


# ---------------------------------------------------------------------------
# Port detection, host side.
#
# Two sources, because neither sees the whole picture. A server the agent
# started itself is a descendant of its tmux pane, so walking parent pids
# attributes it. A Compose-published port is held by docker-proxy, whose parent
# is dockerd — no walk reaches a pane from there, so the Compose project label
# has to answer instead. On a real box the second case is the common one.
# ---------------------------------------------------------------------------

PROBE_TIMEOUT = 5     # a wedged docker ps must never hang the machine asking
ANCESTRY_MAX = 32     # a pid chain longer than this is a cycle, not a tree

PORT_DEFAULTS = {"auto": True, "interval": 10, "bind": "127.0.0.1",
                 "min": 1024, "ignore": (), "max_per_session": 12}


def ports_config() -> dict:
    """The ports.* config block with defaults filled in."""
    cfg = dict(PORT_DEFAULTS)
    configured = load_config().get("ports")
    if isinstance(configured, dict):
        for key in PORT_DEFAULTS:
            if configured.get(key) is not None:
                cfg[key] = configured[key]
    ignore = cfg["ignore"]
    cfg["ignore"] = {int(p) for p in ignore if str(p).isdigit()} \
        if isinstance(ignore, (list, tuple, set)) else set()
    return cfg


def pane_roots() -> dict[int, tuple[str, str, str]]:
    """pane pid -> (@luv_id, @luv_workspace, session name) per luv session here.

    Panes without an @luv_workspace are the user's own tmux sessions and are
    left out: forwarding whatever those happen to be listening on is not
    something anybody asked for.

    The session name rides along because the machine holding the ports is the
    only one that knows it for certain — a session dispatched a moment ago is
    still called luv-pending-<id> in the registry and gets renamed on arrival.
    """
    r = run(["tmux", "list-panes", "-a", "-F",
             "#{@luv_id}|#{@luv_workspace}|#{session_name}|#{pane_pid}"],
            timeout=PROBE_TIMEOUT)
    roots: dict[int, tuple[str, str, str]] = {}
    if r.returncode != 0:
        return roots
    for line in r.stdout.splitlines():
        parts = line.split("|")
        if len(parts) == 4 and parts[1] and parts[3].isdigit():
            roots[int(parts[3])] = (parts[0], parts[1], parts[2])
    return roots


def process_tree() -> dict[int, tuple[int, str]]:
    """pid -> (ppid, command name) for every process we can see.

    ps rather than /proc so a macOS host works from the same code, and the
    command name comes along for free — it is the label for whatever a listener
    turns out to be.
    """
    r = run(["ps", "-eo", "pid=,ppid=,comm="], timeout=PROBE_TIMEOUT)
    tree: dict[int, tuple[int, str]] = {}
    for line in r.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            tree[int(parts[0])] = (int(parts[1]), parts[2].strip())
    return tree


def owning_pane(pid: int, tree: dict[int, tuple[int, str]],
                roots: dict[int, tuple[str, str, str]]) -> tuple[str, str, str] | None:
    """Walk a listener's ancestry to the luv pane that owns it, or None.

    'node' is four or five hops below the pane — npm, a shell, the agent — and
    only the top of that chain identifies a session.
    """
    for _ in range(ANCESTRY_MAX):
        if pid in roots:
            return roots[pid]
        parent = tree.get(pid)
        if parent is None or parent[0] <= 1:
            return None
        pid = parent[0]
    return None


SS_LISTENER_RE = re.compile(r'users:\(\("([^"]+)",pid=(\d+)')


def parse_ss(output: str) -> list[tuple[int, int, str]]:
    """(port, pid, command) from `ss -ltnp` output.

    ss fills in users:(...) only for our own processes, which is exactly the
    filter wanted: root's services and other people's drop out without needing
    privileges to look at them in the first place.
    """
    found = []
    for line in output.splitlines():
        m = SS_LISTENER_RE.search(line)
        if not m:
            continue
        fields = line.split()
        if len(fields) < 4:
            continue
        port = fields[3].rsplit(":", 1)[-1]
        if port.isdigit():
            found.append((int(port), int(m.group(2)), m.group(1)))
    return found


def parse_lsof(output: str) -> list[tuple[int, int, str]]:
    """(port, pid, command) from `lsof -F pcn` output — the macOS fallback.

    One field per line, tagged by its first character, grouped process-then-files.
    """
    found, pid, comm = [], None, ""
    for line in output.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p" and value.isdigit():
            pid, comm = int(value), ""
        elif tag == "c":
            comm = value
        elif tag == "n" and pid is not None:
            port = value.rsplit(":", 1)[-1]
            if port.isdigit():
                found.append((int(port), pid, comm))
    return found


def host_listeners() -> list[tuple[int, int, str]]:
    """Every listening TCP socket owned by a process of ours."""
    r = run(["ss", "-ltnpH"], timeout=PROBE_TIMEOUT)
    if r.returncode == 0:
        return parse_ss(r.stdout)
    # -H is newer than ss itself; an older build prints a header rather than
    # accepting the flag, and search-per-line skips it without extra work.
    r = run(["ss", "-ltnp"], timeout=PROBE_TIMEOUT)
    if r.returncode == 0:
        return parse_ss(r.stdout)
    r = run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-F", "pcn"],
            timeout=PROBE_TIMEOUT)
    return parse_lsof(r.stdout) if r.returncode == 0 else []


DOCKER_PS_FMT = ('{{.Label "com.docker.compose.project"}}|'
                 '{{.Label "com.docker.compose.service"}}|{{.Ports}}')
# Only a published port has a '->'; a bare '9009/tcp' is exposed to the compose
# network and has nothing on the host to point a tunnel at.
PUBLISHED_RE = re.compile(r":(\d+)->\d+/tcp")


def compose_normalize(name: str) -> str:
    """How Compose rewrites a project name it was handed."""
    return re.sub(r"[^a-z0-9_-]", "", name.lower())


def compose_aliases(workspace: str) -> set[str]:
    """Every Compose project name that could mean this workspace.

    luv's own start_docker passes -p luv-<workspace>, but an agent that runs
    `docker compose up` itself gets a project named after the directory it is
    standing in — which is the workspace folder. Both are luv's work, and in
    practice the second is the one that turns up.
    """
    names = {workspace, f"luv-{workspace}"}
    return names | {compose_normalize(n) for n in names}


def docker_listeners(workspaces: set[str]) -> dict[int, tuple[str, str]]:
    """host port -> (workspace, compose service) for luv's Compose stacks."""
    if not workspaces or not shutil.which("docker"):
        return {}
    r = run(["docker", "ps", "--format", DOCKER_PS_FMT], timeout=PROBE_TIMEOUT)
    if r.returncode != 0:
        return {}
    owner = {}
    for ws in sorted(workspaces):
        for alias in compose_aliases(ws):
            owner.setdefault(alias, ws)
    found: dict[int, tuple[str, str]] = {}
    for line in r.stdout.splitlines():
        project, _, rest = line.partition("|")
        service, _, ports = rest.partition("|")
        ws = owner.get(project.strip())
        if not ws:
            continue
        # The same mapping is listed once for IPv4 and again for IPv6; keying by
        # host port collapses the pair.
        for port in PUBLISHED_RE.findall(ports):
            found[int(port)] = (ws, service.strip() or "docker")
    return found


def cmd_listening() -> None:
    """Print '<luv_id>|<workspace>|<session>|<port>|<label>' for every port a luv
    session on this machine is listening on.

    For the dispatcher rather than for people, like --where and --paths: only
    the machine running the servers can see them, and only it can say which tmux
    pane each one hangs off.
    """
    roots = pane_roots()
    if not roots:
        return
    workspaces = {ws for _, ws, _ in roots.values()}
    owners = {ws: (luv_id, session) for luv_id, ws, session in roots.values()}

    found: dict[int, tuple[str, str]] = {}
    tree = process_tree()
    for port, pid, comm in host_listeners():
        owner = owning_pane(pid, tree, roots)
        if owner:
            found.setdefault(port, (owner[1], comm))

    # Docker last, and overriding: ss shows a published port as docker-proxy,
    # which the ancestry walk either missed or attributed to nothing useful. The
    # Compose service name is the better answer for a person reading it, too.
    found.update(docker_listeners(workspaces))

    cfg = ports_config()
    for port in sorted(found):
        if port < cfg["min"] or port in cfg["ignore"]:
            continue
        workspace, label = found[port]
        luv_id, session = owners.get(workspace, ("", ""))
        print(f"{luv_id}|{workspace}|{session}|{port}|{label}")


def host_label(hc: dict | None) -> str:
    return hc["host"] if hc else "local"


def luv_command(hc: dict | None, luv_args: list[str],
                env: dict[str, str] | None = None) -> str:
    """A shell command invoking luv on `hc`, with luv's private env vars set."""
    env = {"_LUV_INNER": "1", **(env or {})}
    if hc and hc.get("dir"):
        env["_LUV_PRS_DIR"] = str(hc["dir"])
    binary = str(hc.get("luv_bin") or "luv") if hc else (shutil.which("luv") or "luv")
    return shlex.join(["env"] + [f"{k}={v}" for k, v in env.items()]
                      + [binary] + luv_args)


def remote_paths(hc: dict | None) -> tuple[Path, Path]:
    """($HOME, workspace root) on a host, both absolute."""
    if hc is None:
        return Path.home(), PRS_DIR
    r = ssh_run(hc, luv_command(hc, ["--paths"]))
    lines = [l.strip() for l in r.stdout.splitlines() if l.strip()]
    if r.returncode != 0 or len(lines) < 2:
        die(f"could not read paths from {hc['host']}: "
            f"{r.stderr.strip() or 'no answer'}")
    return Path(lines[-2]), Path(lines[-1])


# ---------------------------------------------------------------------------
# Port forwarding, this machine's side.
#
# One multiplexed ssh connection per host, kept apart from every other call
# site. Forwards can then be added and dropped on a connection that is already
# up — which is the whole trick, because a server the agent starts ten minutes
# in cannot be named on the command line that opened the session.
# ---------------------------------------------------------------------------

FORWARD_TIMEOUT = 10
_forward_warned: set[str] = set()


def query_ports(hc: dict | None) -> list[dict] | None:
    """What luv sessions on a host are listening on. None if it never answered.

    Mirrors query_tmux, the None included: a host that is merely unreachable
    must not read as a host whose servers all stopped, or the next sync would
    tear down every forward it has.

    A host running a luv too old to know --listening fails its own argument
    parse and prints nothing, which arrives here as "no ports" — the way
    --where already degrades.
    """
    r = ssh_run(hc, luv_command(hc, ["--listening"]))
    if hc is not None and r.returncode == 255:
        return None
    rows = []
    for line in r.stdout.strip().splitlines():
        parts = line.split("|")
        if len(parts) == 5 and parts[3].isdigit():
            rows.append({"id": parts[0], "workspace": parts[1], "session": parts[2],
                         "port": int(parts[3]), "label": parts[4]})
    return rows


def tunnel_socket(hc: dict) -> Path:
    """Control socket for this host's forwarding connection.

    Hashed rather than named after the host for two reasons: ControlPath has to
    fit a sockaddr_un (104 bytes on macOS, and a long host name plus a long home
    directory gets there), and the same host reached with a different key or
    port is a different connection.
    """
    ident = "|".join(str(hc.get(k) or "")
                     for k in ("host", "port", "identity_file"))
    return TUNNEL_DIR / f"{hashlib.sha256(ident.encode()).hexdigest()[:12]}.sock"


def tunnel_state_path(hc: dict) -> Path:
    return tunnel_socket(hc).with_suffix(".json")


def load_tunnel_state(hc: dict) -> dict[int, int]:
    """local port -> remote port for what this master is believed to carry.

    Kept beside the socket rather than derived: ssh can be asked whether a
    master is alive but not what it is forwarding, and re-adding a forward that
    already exists fails noisily.
    """
    try:
        data = json.loads(tunnel_state_path(hc).read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    pairs = data.get("forwards") if isinstance(data, dict) else None
    state = {}
    for pair in pairs if isinstance(pairs, list) else []:
        if isinstance(pair, list) and len(pair) == 2 and \
                all(isinstance(p, int) for p in pair):
            state[pair[0]] = pair[1]
    return state


def save_tunnel_state(hc: dict, forwards: dict[int, int]) -> None:
    TUNNEL_DIR.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        tunnel_state_path(hc).write_text(json.dumps(
            {"host": hc["host"], "forwards": sorted(forwards.items())}) + "\n")


def tunnel_ctl(hc: dict, op: list[str],
               timeout: float = FORWARD_TIMEOUT) -> subprocess.CompletedProcess:
    """Run an ssh control command against this host's forwarding master."""
    return run(ssh_base(hc, batch=True, control=str(tunnel_socket(hc)),
                        control_op=op), timeout=timeout)


def tunnel_alive(hc: dict) -> bool:
    return tunnel_ctl(hc, ["-O", "check"], timeout=5).returncode == 0


def tunnel_up(hc: dict) -> bool:
    """Ensure the forwarding master is connected. False when it will not come up."""
    if tunnel_alive(hc):
        return True
    TUNNEL_DIR.mkdir(parents=True, exist_ok=True)
    # -f backgrounds ssh once the connection is made, so this returns quickly
    # and the check below is what actually confirms it.
    run(ssh_base(hc, batch=True, control=str(tunnel_socket(hc)), master=True),
        timeout=20)
    return tunnel_alive(hc)


def tunnel_down(hc: dict) -> None:
    """Drop the master and every forward it carries."""
    tunnel_ctl(hc, ["-O", "exit"], timeout=5)
    with contextlib.suppress(OSError):
        tunnel_state_path(hc).unlink()


def forward_spec(cfg: dict, local: int, remote: int) -> str:
    """The -L argument for one forward.

    The far end is always loopback *on the host*, which is right whether the
    server bound 127.0.0.1 or 0.0.0.0. The near end defaults to loopback too:
    binding 0.0.0.0 would republish someone's dev server onto the LAN.
    """
    return f"{cfg['bind']}:{local}:127.0.0.1:{remote}"


def forward_change(hc: dict, cfg: dict, op: str, local: int, remote: int) -> bool:
    """Add or cancel one forward on the live master."""
    r = tunnel_ctl(hc, ["-O", op, "-L", forward_spec(cfg, local, remote)])
    if r.returncode != 0 and op == "forward":
        host = hc["host"]
        if "administratively prohibited" in r.stderr.lower() \
                and host not in _forward_warned:
            _forward_warned.add(host)
            print(f"luv: warning: {host} refuses port forwarding "
                  "(sshd AllowTcpForwarding is off)", file=sys.stderr)
        return False
    return True


def port_free(port: int, bind: str) -> bool:
    """Whether we could listen on this port right now."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((bind, port))
            return True
        except OSError:
            return False


def pick_local_port(remote: int, taken: set[int], bind: str) -> int | None:
    """A free local port for a remote one, its own number where possible.

    Mirroring is what makes this pleasant: the URL the agent prints in its own
    logs is then the URL that works here. The walk upward covers the second
    session that also wants :3000, and the ephemeral fallback the case where a
    whole block is spoken for.
    """
    for candidate in range(remote, min(remote + 51, 65536)):
        if candidate not in taken and port_free(candidate, bind):
            return candidate
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((bind, 0))
            return s.getsockname()[1]
        except OSError:
            return None


def session_ports(entry: dict, rows: list[dict]) -> list[dict]:
    """Detected listeners belonging to one session, lowest port first."""
    ws, sid = entry.get("workspace"), entry.get("id")
    mine = {}
    for row in rows:
        if (ws and row["workspace"] == ws) or (sid and row["id"] and row["id"] == sid):
            mine.setdefault(row["port"], row)
    return [mine[p] for p in sorted(mine)]


def desired_forwards(entry: dict, rows: list[dict], cfg: dict,
                     taken: set[int], established: dict[int, int]) -> list[dict]:
    """The forward list this session should have.

    A local port already in use for this same remote port is kept rather than
    reallocated — a URL that worked a minute ago should still work. `established`
    is what says a busy local port is busy because *we* are already forwarding
    it, which no bind test can distinguish.
    """
    prior = {f["remote"]: f["local"] for f in entry.get("forwards") or []
             if isinstance(f, dict)
             and isinstance(f.get("remote"), int) and isinstance(f.get("local"), int)}
    out = []
    for row in session_ports(entry, rows):
        if len(out) >= cfg["max_per_session"]:
            break
        remote = row["port"]
        local = prior.get(remote)
        reusable = (local is not None and local not in taken
                    and (established.get(local) == remote or port_free(local, cfg["bind"])))
        if not reusable:
            local = pick_local_port(remote, taken, cfg["bind"])
        if local is None:
            continue
        taken.add(local)
        out.append({"remote": remote, "local": local, "label": row["label"]})
    return out


def sync_forwards(sessions: list[dict], detected: dict[str, list[dict] | None],
                  identity: str | None = None,
                  opt_in=lambda s: False) -> list[tuple[dict, list[dict]]]:
    """Bring every host's forwards in line with what its sessions are listening on.

    Mutates each session's "forwards" in place and returns (session, new
    forwards) for whatever appeared this pass, so a caller can announce it.

    `opt_in` decides whether a session that holds no forwards yet should get
    them. Sessions that already have some always keep being maintained, which is
    what lets a forward outlive detaching. Everything else needs asking for: a
    busy box carries dozens of sessions and they should not all get a piece of
    this machine's port space uninvited.
    """
    cfg = ports_config()
    # Probed hosts join the set even with no sessions on them: that is how a
    # master outlives the last session it was carrying forwards for, and it must
    # be told to go away rather than lingering until the machine reboots.
    hosts = {s.get("host") or "" for s in sessions if s.get("host")}
    hosts |= {h for h in detected if h and detected[h] is not None}
    hcs = {h: resolve_host(h, identity) for h in hosts}
    established = {h: load_tunnel_state(hc) for h, hc in hcs.items() if hc}

    taken: set[int] = set()
    want_by_host: dict[str, dict[int, int]] = {h: {} for h in hosts}
    fresh: list[tuple[dict, list[dict]]] = []

    # Stable order so two runs with the same input allocate the same ports.
    for s in sorted(sessions, key=lambda s: str(s.get("id") or "")):
        host = s.get("host") or ""
        rows = detected.get(host)
        held = [f for f in s.get("forwards") or [] if isinstance(f, dict)]
        # A local session needs no tunnel, and an unreachable host keeps what it
        # has — but either way those local ports are still spoken for.
        if not host or not hcs.get(host) or rows is None:
            taken |= {f["local"] for f in held if isinstance(f.get("local"), int)}
            continue
        if not held and not opt_in(s):
            continue
        want = desired_forwards(s, rows, cfg, taken, established[host])
        before = {(f.get("remote"), f.get("local")) for f in held}
        s["forwards"] = want
        s["ports_checked"] = int(time.time())
        added = [f for f in want if (f["remote"], f["local"]) not in before]
        if added:
            fresh.append((s, added))
        want_by_host[host].update({f["local"]: f["remote"] for f in want})

    for host, want in want_by_host.items():
        hc = hcs.get(host)
        if hc is None or detected.get(host) is None:
            continue
        state = established[host]
        if want == state:
            continue
        if want and not tunnel_up(hc):
            continue
        for local, remote in sorted(state.items()):
            if want.get(local) != remote:
                forward_change(hc, cfg, "cancel", local, remote)
        applied = {l: r for l, r in state.items() if want.get(l) == r}
        for local, remote in sorted(want.items()):
            if state.get(local) == remote or forward_change(hc, cfg, "forward", local, remote):
                applied[local] = remote
        if applied:
            save_tunnel_state(hc, applied)
        else:
            tunnel_down(hc)
    return fresh


def announce_forwards(hc: dict, session: str, forwards: list[dict],
                      added: list[dict]) -> None:
    """Tell an attached session about its forwards, without touching the tty.

    display-message lands on tmux's status line, underneath whatever full-screen
    UI the agent is drawing — the one way to say something to a person who is
    mid-session. @luv_ports is set regardless, for anyone who would rather have
    it permanently in their own status-right.
    """
    argv = ssh_base(hc, batch=True, control=str(tunnel_socket(hc)))
    summary = " ".join(f"{f['local']}:{f['label']}" for f in forwards)
    cmds = [["tmux", "set-option", "-t", session, "@luv_ports", summary]]
    if added:
        note = ", ".join(f"localhost:{f['local']} → {f['label']}" for f in added)
        cmds.append(["tmux", "display-message", "-t", session, f"luv: {note}"])
    for cmd in cmds:
        run(argv + [remote_shell(shlex.join(cmd))], timeout=5)


PORT_KEYS = ("forwards", "ports", "ports_checked")  # cached in sessions.json


def log_ports(message: str) -> None:
    """Append to ~/.luv/ports.log — the watcher's only outlet.

    It runs while a full-screen agent UI owns the terminal, so it has nowhere
    else to put a complaint.
    """
    with contextlib.suppress(OSError):
        LUV_DIR.mkdir(parents=True, exist_ok=True)
        with PORTS_LOG.open("a") as fh:
            fh.write(f"{int(time.time())} {message}\n")


def merge_port_state(rows: list[dict]) -> None:
    """Write the port columns back, re-reading under the lock.

    The same care the PR cache takes: another luv may have appended a session
    while we were talking to the host, and only these keys are ours to replace.
    """
    cached = {s["id"]: {k: s[k] for k in PORT_KEYS if k in s}
              for s in rows if s.get("id")}
    cached = {sid: keys for sid, keys in cached.items() if keys}
    if not cached:
        return
    with session_lock():
        stored = load_sessions()
        for s in stored:
            if s.get("id") in cached:
                s.update(cached[s["id"]])
        save_sessions(stored)


def attach_port_info(rows: list[dict], detected: dict[str, list[dict] | None]) -> bool:
    """Record the ports each session was seen listening on. Reports any change.

    Separate from forwarding on purpose — `luv ls` shows this for every session
    without opening a tunnel to any of them.
    """
    changed = False
    for s in rows:
        found = detected.get(s.get("host") or "")
        if found is None:
            continue
        ports = [row["port"] for row in session_ports(s, found)]
        changed = changed or s.get("ports") != ports
        s["ports"] = ports
    return changed


def port_watch(hc: dict | None, sid: str | None = None,
               session: str | None = None):
    """A watcher callback for an attached remote session, or None.

    Local sessions have nothing to forward: the servers are already here.
    """
    if hc is None or not ports_config()["auto"]:
        return None

    def mine(s: dict) -> bool:
        # @luv_id first: a just-dispatched session is still luv-pending-<id>
        # here and its name is the one thing about to change.
        if sid and s.get("id") == sid:
            return True
        return bool(session) and s.get("session") == session

    def step() -> None:
        host = hc["host"]
        rows = query_ports(hc)
        if rows is None:
            return
        stored = load_sessions()
        watched = {s.get("id") for s in stored
                   if (s.get("host") or "") == host and mine(s) and s.get("id")}
        if not watched:
            return
        fresh = sync_forwards(stored, {host: rows},
                              opt_in=lambda s: s.get("id") in watched)
        attach_port_info([s for s in stored if s.get("id") in watched],
                         {host: rows})
        merge_port_state(stored)
        for entry, added in fresh:
            name = next((r["session"] for r in session_ports(entry, rows) if r["session"]),
                        entry.get("session"))
            if name:
                announce_forwards(hc, name, entry.get("forwards") or [], added)

    return step


def start_port_watcher(step) -> None:
    """Run `step` on a timer for as long as this process lives.

    A daemon thread, so exiting never waits on it, and silent by construction:
    the terminal belongs to a full-screen agent UI and anything written there
    from here would land in the middle of somebody's redraw. What it has to say
    goes to tmux's status line; what goes wrong goes to the log.
    """
    interval = max(2, int(ports_config()["interval"] or 10))

    def loop() -> None:
        while True:
            time.sleep(interval)
            try:
                step()
            except Exception as exc:  # a watcher must never take the session down
                log_ports(f"watch failed: {exc!r}")

    threading.Thread(target=loop, daemon=True).start()


def tar_send_argv(hc: dict | None, cwd: Path, members: list[str]) -> list[str]:
    """argv that writes a tar of `members` to stdout.

    Never a TTY: ssh -t would translate newlines and corrupt the stream.
    """
    if hc is None:
        return ["tar", "-C", str(cwd), "-czf", "-"] + members
    inner = shlex.join(["tar", "-C", str(cwd), "-czf", "-"] + members)
    return ssh_base(hc, batch=True) + [remote_shell(inner)]


def tar_recv_argv(hc: dict | None, dest: Path) -> list[str]:
    """argv that unpacks a tar arriving on stdin into `dest`."""
    if hc is None:
        dest.mkdir(parents=True, exist_ok=True)
        return ["tar", "-C", str(dest), "-xzf", "-"]
    quoted = shlex.quote(str(dest))
    return ssh_base(hc, batch=True) + [
        remote_shell(f"mkdir -p {quoted} && tar -C {quoted} -xzf -")]


def stream_copy(src_hc: dict | None, src_dir: Path, members: list[str],
                dst_hc: dict | None, dst_dir: Path) -> None:
    """Relay a tar stream from one machine to another through this one.

    Going through the laptop means the two machines never need credentials for
    each other — you already hold keys to both.
    """
    if not members:
        return
    send = subprocess.Popen(tar_send_argv(src_hc, src_dir, members),
                            stdout=subprocess.PIPE)
    recv = subprocess.Popen(tar_recv_argv(dst_hc, dst_dir), stdin=send.stdout)
    send.stdout.close()  # let the sender see EPIPE if the receiver dies first
    recv_rc = recv.wait()
    send_rc = send.wait()
    if send_rc != 0 or recv_rc != 0:
        die(f"transfer failed (tar exit {send_rc} sending, {recv_rc} receiving)")


def claude_project_slug(path: Path) -> str:
    """Claude keys transcripts by cwd with non-alphanumerics replaced by '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def agent_state_members(hc: dict | None, agent: str, ws: Path,
                        home: Path) -> list[str]:
    """Transcript paths for a workspace, relative to $HOME.

    Best-effort by nature — it depends on each agent's on-disk layout, so an
    empty result is reported and tolerated rather than treated as a failure.
    """
    if agent == "codex":
        # Codex has no path-derived directory, so the rollouts have to be found
        # by the workspace path recorded inside them.
        r = ssh_run(hc, f"cd {shlex.quote(str(home))} && "
                        f"grep -rlF {shlex.quote(str(ws))} .codex/sessions "
                        "--include='rollout-*.jsonl' 2>/dev/null")
        return [l.strip() for l in r.stdout.splitlines() if l.strip()]
    member = f".claude/projects/{claude_project_slug(ws)}"
    r = ssh_run(hc, f"test -d {shlex.quote(str(home / member))}")
    return [member] if r.returncode == 0 else []


def rewrite_script(files_expr: str, old: str, new: str) -> str:
    """Shell that rewrites one path to another across files.

    Not `sed -i`: GNU and BSD sed disagree about its argument, and the laptop
    half of a handover is usually a Mac.
    """
    if old == new:
        return "true"
    return (f'for f in {files_expr}; do [ -f "$f" ] || continue; '
            f'sed {shlex.quote(f"s|{old}|{new}|g")} "$f" > "$f.luvtmp" '
            f'&& mv "$f.luvtmp" "$f"; done')


def settle_agent_state(hc: dict | None, agent: str, members: list[str],
                       src_ws: Path, dst_ws: Path, dst_home: Path) -> None:
    """Put copied transcripts where the destination's agent will look for them.

    Both agents key on the absolute workspace path, and the two machines rarely
    agree on it — so the files move to the destination's own project directory
    and the path recorded inside them is rewritten to match.
    """
    if agent == "codex":
        files = " ".join(shlex.quote(str(dst_home / m)) for m in members)
        # Freshen them so 'codex resume --last' prefers the session that just
        # arrived over whatever else this machine worked on recently.
        script = f"{rewrite_script(files, str(src_ws), str(dst_ws))}; touch {files}"
    else:
        src_dir = dst_home / ".claude" / "projects" / claude_project_slug(src_ws)
        dst_dir = dst_home / ".claude" / "projects" / claude_project_slug(dst_ws)
        s, d = shlex.quote(str(src_dir)), shlex.quote(str(dst_dir))
        move = (f'if [ {s} != {d} ]; then mkdir -p {d} && mv {s}/* {d}/ '
                f'2>/dev/null; rmdir {s} 2>/dev/null; fi')
        script = f'{move}; {rewrite_script(f"{d}/*.jsonl", str(src_ws), str(dst_ws))}'
    r = ssh_run(hc, script)
    if r.returncode != 0:
        print(f"luv: warning: could not finish placing agent state "
              f"({r.stderr.strip()})", file=sys.stderr)


def workspace_git_state(hc: dict | None, ws: Path) -> tuple[str, str]:
    """(HEAD sha, count of dirty paths) — enough to tell a copy went wrong."""
    r = ssh_run(hc, f"cd {shlex.quote(str(ws))} && git rev-parse HEAD && "
                    "git status --porcelain | wc -l")
    parts = r.stdout.split()
    return (parts[0], parts[1]) if r.returncode == 0 and len(parts) >= 2 else ("", "")


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
PR_KEYS = ("pr_number", "pr_url", "pr_state", "pr_checked")  # cached in sessions.json


def fetch_pr(org: str, repo: str, branch: str) -> dict | None:
    """The PR a workspace is producing, found by its own head branch.

    Deliberately a head query and nothing else: asking whether PR #number exists
    would happily return a stranger's PR whenever someone took that number
    between luv reserving the folder and the agent pushing. Sessions opened from
    an existing PR carry pr_hint instead — see attach_pr_links.

    `gh pr list` rather than the REST endpoint because its --head takes a bare
    branch name. REST wants `head={owner}:{branch}`, and the owner luv recorded
    is whatever you typed — which stops matching the moment the org is renamed
    or the repo is transferred.
    """
    r = run(["gh", "pr", "list", "--repo", f"{org}/{repo}",
             "--head", branch, "--state", "all", "--limit", "1",
             "--json", "number,url,state"], timeout=PR_TIMEOUT)
    if r.returncode != 0:
        return None
    try:
        prs = json.loads(r.stdout)
    except json.JSONDecodeError:
        return None
    if not isinstance(prs, list) or not prs or not prs[0].get("url"):
        return None
    return {"number": prs[0].get("number"), "url": prs[0]["url"],
            "state": prs[0].get("state")}


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
            s.setdefault("pr_state", None)  # only the head query reports state
            continue
        branch = workspace_branch(repo, s.get("workspace") or "") if repo else None
        if not (org and repo and branch):
            continue
        ttl = PR_TTL_OK if s.get("pr_url") else PR_TTL_MISS
        if now - int(s.get("pr_checked") or 0) < ttl:
            continue
        stale.append((s, org, repo, branch))

    if not stale:
        return changed
    if not shutil.which("gh"):
        print("luv: warning: 'gh' not found — PR column shows last known state",
              file=sys.stderr)
        return changed

    found = fan_out(lambda item: fetch_pr(*item[1:]), stale)
    for (s, *_), pr in zip(stale, found):
        s["pr_number"] = pr["number"] if pr else None
        s["pr_url"] = pr["url"] if pr else None
        s["pr_state"] = pr["state"] if pr else None
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
        stored = load_sessions()
        known = {s.get("host") or "" for s in stored}
        sessions, unreachable = reconcile(stored, identity)
        save_sessions(sessions)
    for host in sorted(unreachable):
        # A host with entries has a last known state to fall back on; one we
        # only scanned because it is configured has nothing to show at all.
        detail = ("showing last known state" if host in known
                  else "its sessions are not listed")
        print(f"luv: warning: {host or 'local'} unreachable — {detail}",
              file=sys.stderr)
    return sessions, unreachable


def session_sort_key(s: dict) -> int:
    return int(s.get("activity") or s.get("last_seen") or s.get("created") or 0)


def ports_cell(s: dict, limit: int = 4) -> str:
    """Detected ports for the session table, kept short.

    PROMPT is the one elastic column, so every port listed here is width taken
    off it. Four and an overflow count is enough to see that something is up.
    """
    ports = [p for p in (s.get("ports") or []) if isinstance(p, int)]
    if not ports:
        return "-"
    shown = ",".join(str(p) for p in ports[:limit])
    return shown if len(ports) <= limit else f"{shown},+{len(ports) - limit}"


def print_sessions(rows: list[dict]) -> None:
    """Print the session table, truncating the prompt to the terminal width."""
    headers = ("HOST", "SESSION", "WORKSPACE", "AGENT", "ATTACHED", "ACTIVE",
               "PR", "PORTS", "PROMPT")
    last = len(headers) - 1  # PROMPT is the elastic column: truncated, not padded
    pr_col = last - 2
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
            ports_cell(s),
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
    # Detected only — listing sessions must not open a tunnel to every one of
    # them. `luv ports <repo> <n>` is where forwarding is asked for.
    if "--no-ports" not in args and attach_port_info(
            rows, probe_ports(sessions, identity, host_filter)):
        merge_port_state(rows)
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


def probe_ports(sessions: list[dict], identity: str | None = None,
                host_filter: str | None = None) -> dict[str, list[dict] | None]:
    """Ask every known host what its luv sessions are listening on.

    One round trip per host, concurrently, the same shape as reconcile.
    """
    hosts = known_hosts(sessions, host_filter)
    return dict(zip(hosts, fan_out(
        lambda h: query_ports(resolve_host(h, identity) if h else None), hosts)))


def port_rows(sessions: list[dict],
              detected: dict[str, list[dict] | None]) -> list[dict]:
    """One row per port, whether or not it is forwarded."""
    out = []
    for s in sorted(sessions, key=session_sort_key, reverse=True):
        found = detected.get(s.get("host") or "") or []
        forwards = {f["remote"]: f for f in (s.get("forwards") or [])
                    if isinstance(f, dict) and isinstance(f.get("remote"), int)}
        seen = set()
        for row in session_ports(s, found):
            seen.add(row["port"])
            fwd = forwards.get(row["port"])
            out.append({"host": s.get("host") or "local",
                        "workspace": s.get("workspace") or "-",
                        "remote": row["port"],
                        "local": fwd.get("local") if fwd else None,
                        "label": row["label"] or "-",
                        "state": "up" if fwd else "detected"})
        # A forward whose server has stopped is worth saying out loud rather
        # than letting the row disappear; --no-sync is where this shows up.
        for remote, fwd in sorted(forwards.items()):
            if remote not in seen:
                out.append({"host": s.get("host") or "local",
                            "workspace": s.get("workspace") or "-",
                            "remote": remote, "local": fwd.get("local"),
                            "label": fwd.get("label") or "-", "state": "stale"})
    return out


def print_ports(rows: list[dict]) -> None:
    """Print the port table, with the local URL as a clickable link."""
    headers = ("HOST", "WORKSPACE", "REMOTE", "LOCAL", "URL", "SERVICE", "STATE")
    url_col = 4
    table, links = [], []
    for r in rows:
        local = r.get("local")
        url = f"http://localhost:{local}" if local and r["state"] == "up" else None
        links.append(url)
        table.append((r["host"], r["workspace"], str(r["remote"]),
                      str(local) if local else "-", url or "-",
                      r["label"], r["state"]))
    widths = [max(len(headers[i]), max((len(t[i]) for t in table), default=0))
              for i in range(len(headers))]
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)).rstrip())
    for row, url in zip(table, links):
        cells = [row[i].ljust(widths[i]) for i in range(len(headers))]
        # Pad outside the escape so the link covers the URL, not trailing space.
        cells[url_col] = (hyperlink(url, row[url_col])
                          + " " * (widths[url_col] - len(row[url_col])))
        print("  ".join(cells).rstrip())


def drop_forwards(matched: list[dict], identity: str | None) -> int:
    """Cancel every forward the matched sessions hold. Returns how many."""
    cfg = ports_config()
    by_host: dict[str, dict[int, int]] = {}
    dropped = 0
    for s in matched:
        for f in s.get("forwards") or []:
            if isinstance(f.get("local"), int) and isinstance(f.get("remote"), int):
                by_host.setdefault(s.get("host") or "", {})[f["local"]] = f["remote"]
                dropped += 1
        s["forwards"] = []
    for host, drop in by_host.items():
        hc = resolve_host(host, identity) if host else None
        if hc is None:
            continue
        state = load_tunnel_state(hc)
        for local, remote in sorted(drop.items()):
            forward_change(hc, cfg, "cancel", local, remote)
            state.pop(local, None)
        # Nothing left to carry means nothing left to keep a connection open for.
        save_tunnel_state(hc, state) if state else tunnel_down(hc)
    return dropped


def cmd_ports(args: list[str], identity: str | None = None) -> None:
    """Show what luv sessions are listening on, and forward it here.

    Naming a session is what opts it into forwarding — a busy host carries
    dozens, and they should not all get a piece of this machine's port space
    uninvited. With no name this refreshes whatever is already forwarded, which
    is how a session stays reachable after you detach.
    """
    host_filter = None
    if "--host" in args:
        idx = args.index("--host")
        if idx + 1 >= len(args):
            die("--host requires a host name")
        host_filter = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    interval = max(2, int(ports_config()["interval"] or 10))
    if "--watch" in args:
        idx = args.index("--watch")
        if idx + 1 < len(args) and args[idx + 1].isdigit():
            interval = max(2, int(args[idx + 1]))
            args = args[:idx + 1] + args[idx + 2:]

    off = "--off" in args
    no_sync = "--no-sync" in args
    watching = "--watch" in args
    args = [a for a in args if a not in ("--off", "--no-sync", "--watch")]
    named = bool(args)

    def pass_once() -> bool:
        sessions, _ = refresh_sessions(identity)
        scoped = [s for s in sessions
                  if not host_filter or s.get("host") == host_filter]
        matched, label = filter_sessions(scoped, args)
        if not matched:
            print(f"luv: no sessions{label}")
            return False

        if off:
            dropped = drop_forwards(matched, identity)
            merge_port_state(matched)
            print(f"luv: dropped {dropped} forward{'' if dropped == 1 else 's'}")
            return False

        detected = probe_ports(sessions, identity, host_filter)
        attach_port_info(sessions, detected)
        if not no_sync:
            opted = {s.get("id") for s in matched if s.get("id")} if named else set()
            sync_forwards(sessions, detected, identity,
                          opt_in=lambda s: s.get("id") in opted)
        merge_port_state(sessions)

        rows = port_rows(matched, detected)
        if not rows:
            print(f"luv: no ports detected{label}")
            return True
        print_ports(rows)
        return True

    if not watching:
        pass_once()
        return
    try:
        while True:
            if not pass_once():
                return
            time.sleep(interval)
            print()
    except KeyboardInterrupt:
        return


def workspace_number(repo: str, folder: str) -> int | None:
    """The number in a workspace folder name, slugged or not."""
    m = workspace_re(repo).match(folder or "")
    return int(m.group(2)) if m else None


def filter_sessions(sessions: list[dict],
                    args: list[str]) -> tuple[list[dict], str]:
    """Narrow by [<repo> [number]], and describe the filter for error messages.

    The number is matched against the workspace's number rather than the whole
    folder name, so a workspace that arrived by handover — still carrying the
    slug of the machine that made it — is found by the number you know it by.
    """
    if not args:
        return sessions, ""
    repo = args[0].rstrip("/").rsplit("/", 1)[-1]
    rows = [s for s in sessions if s.get("repo") == repo]
    label = f" for '{repo}'"
    if len(args) > 1 and args[1].isdigit():
        number = int(args[1])
        label = f" for '{repo}' {number}"
        rows = [s for s in rows
                if workspace_number(repo, s.get("workspace") or "") == number]
    return rows, label


def choose_session(rows: list[dict]) -> dict:
    """Prompt for one of several sessions."""
    print(f"luv: {len(rows)} live sessions:")
    print_sessions(rows)
    print()
    for i, s in enumerate(rows, 1):
        print(f"  {i}) {s.get('session')}  ({s.get('host') or 'local'})")
    raw = input("Choice [1]: ").strip() or "1"
    try:
        idx = int(raw)
    except ValueError:
        die(f"invalid choice: '{raw}'")
    if not 1 <= idx <= len(rows):
        die(f"invalid choice: {idx}")
    return rows[idx - 1]


def rm_workspace(hc: dict | None, session: str | None, workspace: str) -> str | None:
    """Kill the tmux session and delete its folder on `hc`. Returns an error.

    The workspace name is checked for a trailing number before it goes anywhere
    near `rm -rf`: this runs unattended, over ssh, against a directory full of
    other people's work.
    """
    if folder_number(workspace) is None:
        return f"'{workspace}' is not a {{repo}}-[{{machine}}-]{{N}} workspace"
    steps = []
    if session:
        steps.append(f"tmux kill-session -t {shlex.quote(session)} 2>/dev/null")
    steps.append(f"rm -rf -- {remote_prs_dir(hc)}/{shlex.quote(workspace)}")
    r = ssh_run(hc, "; ".join(steps))
    if hc is not None and r.returncode == 255:
        return "host unreachable"
    if r.returncode != 0:
        return r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "delete failed"
    return None


def workspace_exists(hc: dict | None, workspace: str) -> bool | None:
    """Whether the folder is on the host. None when the host never answered."""
    r = ssh_run(hc, f"test -d {remote_prs_dir(hc)}/{shlex.quote(workspace)}")
    if hc is not None and r.returncode == 255:
        return None
    return r.returncode == 0


def orphan_workspaces(hc: dict | None) -> list[str] | None:
    """Workspace folders on a host with no live luv session. None if unreachable.

    These are what `luv ls` cannot show: reconciliation drops a registry entry
    the moment its tmux session dies, but the clone it left behind stays on the
    remote disk forever.
    """
    live = query_tmux(hc)
    if live is None:
        return None
    r = ssh_run(hc, f"ls -1 -- {remote_prs_dir(hc)} 2>/dev/null")
    if hc is not None and r.returncode == 255:
        return None
    running = {t["workspace"] for t in live if t["workspace"]} | {t["session"] for t in live}
    folders = [f.strip() for f in r.stdout.splitlines() if f.strip()]
    return [f for f in folders
            if folder_number(f) is not None
            and f not in running and tmux_session_name(f) not in running]


def cmd_rm(args: list[str], identity: str | None = None, force: bool = False) -> None:
    """Tear a session down: kill its tmux, delete its folder, forget the entry."""
    host_filter = None
    if "--host" in args:
        idx = args.index("--host")
        if idx + 1 >= len(args):
            die("--host requires a host name")
        host_filter = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    merged, dead = "--merged" in args, "--dead" in args
    targets = [a for a in args if not a.startswith("-")]
    if not (targets or merged or dead):
        die("usage: luv rm <session|workspace>... | --merged | --dead [--host H]")

    sessions, _ = refresh_sessions(identity)
    scope = [s for s in sessions if host_filter is None or s.get("host") == host_filter]

    # (host, session, workspace, label) — session is None for an orphaned folder.
    doomed: list[tuple[str, str | None, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def take(host: str, session: str | None, workspace: str | None, label: str) -> None:
        if not workspace or (host, workspace) in seen:
            return
        seen.add((host, workspace))
        doomed.append((host, session, workspace, label))

    for target in targets:
        hits = [s for s in scope if target in (s.get("session"), s.get("workspace"))]
        if hits:
            for s in hits:
                take(s.get("host") or "", s.get("session"), s.get("workspace"), "named")
            continue
        # Reconciliation drops an entry the moment its tmux session dies, so a
        # finished session — the usual thing you want gone — has already left
        # the registry by the time we look. Fall back to the folder itself.
        # 'luv-myrepo-2' is both a plausible session name and a plausible
        # {repo}-{N}, so try it verbatim before trying it as a session name.
        candidates = [target] + ([target[4:]] if target.startswith("luv-") else [])
        found = False
        for folder in [c for c in candidates if folder_number(c) is not None]:
            for host in known_hosts(sessions, host_filter):
                hc = resolve_host(host, identity) if host else None
                if workspace_exists(hc, folder):
                    take(host, tmux_session_name(folder), folder, "named")
                    found = True
            if found:
                break
        if not found:
            die(f"no session or workspace matching '{target}'"
                + (f" on {host_filter}" if host_filter else ""))

    if merged:
        attach_pr_links(scope)
        for s in scope:
            if (s.get("pr_state") or "").upper() == "MERGED":
                take(s.get("host") or "", s.get("session"), s.get("workspace"),
                     f"PR #{s.get('pr_number')} merged")

    if dead:
        for host in known_hosts(sessions, host_filter):
            hc = resolve_host(host, identity) if host else None
            orphans = orphan_workspaces(hc)
            if orphans is None:
                print(f"luv: warning: {host or 'local'} unreachable — skipped",
                      file=sys.stderr)
                continue
            for folder in orphans:
                take(host, None, folder, "no live session")

    if not doomed:
        print("luv: nothing to remove")
        return

    # An explicit target is its own confirmation; a selector is not — it can
    # sweep up folders on machines you are not looking at.
    if (merged or dead) and not force:
        print(f"luv: about to remove {len(doomed)} workspace"
              f"{'' if len(doomed) == 1 else 's'}:")
        for host, session, workspace, label in doomed:
            print(f"  {host or 'local'}  {workspace}  ({label})")
        if (input("Proceed? [y/N]: ").strip().lower() or "n") not in ("y", "yes"):
            print("luv: aborted")
            return

    removed, failed = [], []
    for host, session, workspace, _ in doomed:
        hc = resolve_host(host, identity) if host else None
        err = rm_workspace(hc, session, workspace)
        if err:
            failed.append((host, workspace, err))
        else:
            removed.append((host, workspace))
            print(f"luv: {host or 'local'} — removed {workspace}")

    gone = {(h, w) for h, w in removed}
    with session_lock():
        keep = [s for s in load_sessions()
                if ((s.get("host") or ""), s.get("workspace")) not in gone]
        save_sessions(keep)

    if failed:
        print("luv: failed:")
        for host, workspace, err in failed:
            print(f"  {host or 'local'} {workspace}: {err}")
    print(f"luv: removed {len(removed)} workspace"
          f"{'' if len(removed) == 1 else 's'}")


def cmd_continue(args: list[str], identity: str | None = None) -> None:
    """Attach to a live luv session; picks or prompts when ambiguous."""
    if args and args[0] == "--list":
        cmd_ls([], identity)
        return

    sessions, _ = refresh_sessions(identity)
    live, label = filter_sessions([s for s in sessions if s.get("live")], args)

    if not live:
        # This is where a hint printed after a crash lands when the agent took
        # the tmux session down with it, so send it on rather than stopping at
        # the bad news: the workspace outlives the session, and -r reopens it.
        named = bool(args) and not args[0].startswith("-")
        die(f"no live luv sessions{label}"
            + (f" — if its workspace is still there: {reopen_hint(args)}"
               if named else ""))
    live.sort(key=session_sort_key, reverse=True)

    # An explicit repo means "the newest one for it".
    target = live[0] if (len(live) == 1 or args) else choose_session(live)

    host = target.get("host")
    attach_session(resolve_host(host, identity) if host else None, target["session"],
                   hint=continue_hint(target.get("repo"), target.get("workspace")))


def start_local_session(workspace: str, luv_args: list[str], meta: dict,
                        attach: bool) -> None:
    """Start a workspace in tmux on this machine, the way dispatch_remote does
    on a remote one. _LUV_INNER keeps it here even with a host configured."""
    tmux_bin = shutil.which("tmux")
    if not tmux_bin:
        die("'tmux' not found in PATH")
    sid = new_session_id()
    session = tmux_session_name(workspace)
    record_session({**meta, "id": sid, "host": None, "session": session,
                    "workspace": workspace, "created": int(time.time())})
    inner = ["env", "_LUV_INNER=1", f"_LUV_ID={sid}",
             shutil.which("luv") or "luv"] + luv_args
    argv = [tmux_bin, "new-session"] + ([] if attach else ["-d"]) + \
           ["-A", "-s", session, "--"] + inner
    hint = continue_hint(meta.get("repo"), workspace)
    print(f"luv: local — {session}")
    if attach:
        hand_over(argv, hint=hint)
    r = subprocess.run(argv)
    if r.returncode != 0:
        die(f"could not start {session}")
    print(f"luv: started detached — attach with: {hint}")


def workspace_origin(hc: dict | None, ws: Path) -> tuple[str, str] | None:
    """(org, repo) from a workspace's origin remote, on either machine."""
    r = ssh_run(hc, f"git -C {shlex.quote(str(ws))} remote get-url origin")
    return parse_github_url(r.stdout) if r.returncode == 0 else None


def handover_target(args: list[str], identity: str | None, from_host: str | None,
                    agent: str) -> dict:
    """The workspace to hand over: a registered session, else a folder on disk.

    The registry only ever learns about sessions luv dispatched to a remote
    host, so a workspace started on this machine is invisible to it — and the
    laptop is the usual source of a handover. An agent that has already exited
    leaves no entry either. Neither is an error: a workspace is movable whether
    or not something is currently running in it.
    """
    sessions, _ = refresh_sessions(identity)
    rows = [s for s in sessions if s.get("live")]
    if from_host is not None:
        want = "" if from_host == "local" else from_host
        rows = [s for s in rows if (s.get("host") or "") == want]
    rows, label = filter_sessions(rows, args)
    if rows:
        rows.sort(key=session_sort_key, reverse=True)
        entry = dict(rows[0] if (len(rows) == 1 or args) else choose_session(rows))
        entry["number"] = workspace_number(entry.get("repo") or "",
                                           entry.get("workspace") or "")
        return entry

    if not args:
        die("no live sessions to hand over; name a workspace: "
            "luv handover <repo> [number] --to <host>")
    repo = args[0].rstrip("/").rsplit("/", 1)[-1]
    number = int(args[1]) if len(args) > 1 and args[1].isdigit() else None

    if from_host is not None and from_host != "local":
        # Nothing running there, but the folder may still be — ask the host.
        if number is None:
            die(f"no live session{label} on {from_host}; "
                f"name the number too: luv handover {repo} <number> --from {from_host}")
        hc = resolve_host(from_host, identity)
        folder_name = resolve_remote_workspace(hc, resolve_org(None), repo, number)
        if folder_name is None:
            die(f"no live session{label} on {from_host} and "
                f"{from_host} could not name a workspace for '{repo}' {number}")
        _, root = remote_paths(hc)
        origin = workspace_origin(hc, root / folder_name)
        return {"host": from_host, "session": tmux_session_name(folder_name),
                "org": origin[0] if origin else resolve_org(None), "repo": repo,
                "workspace": folder_name, "number": number, "agent": agent,
                "prompt": None, "model": None}

    folder = find_workspace(repo, number) if number else find_latest_clone(repo)
    if folder is None:
        die(f"no live session{label} and no local workspace for '{repo}' "
            f"in {PRS_DIR}")
    number = workspace_number(repo, folder.name)
    if number is None:
        die(f"cannot read a workspace number from '{folder.name}'")
    origin = parse_github_remote(str(folder))
    return {"host": None, "session": tmux_session_name(folder.name),
            "org": origin[0] if origin else resolve_org(None),
            "repo": repo, "workspace": folder.name, "number": number,
            "agent": agent, "prompt": None, "model": None}


def cmd_handover(args: list[str], *, identity: str | None = None,
                 to: str | None = None, from_host: str | None = None,
                 agent: str = "claude", force: bool = False, purge: bool = False,
                 no_agent_state: bool = False, attach: bool = True,
                 assume_yes: bool = False) -> None:
    """Move a workspace and its agent's conversation to another machine.

    Everything that can fail is checked before the agent is stopped, so a
    refusal costs nothing. After that point the source folder is still left
    intact, so a failed transfer is always recoverable by restarting it there.
    """
    if not to:
        die("handover needs a destination: "
            "luv handover [<repo> [number]] --to <host>")

    entry = handover_target(args, identity, from_host, agent)
    if not entry.get("workspace") or entry.get("number") is None:
        # A session that hasn't reported its folder yet has nothing to move.
        die(f"'{entry.get('session') or 'that session'}' has no workspace yet — "
            "wait for it to finish cloning, then try again")
    src_hc = resolve_host(entry["host"], identity) if entry.get("host") else None
    dst_hc = None if to == "local" else resolve_host(to, identity)
    if host_label(src_hc) == host_label(dst_hc):
        die(f"source and destination are the same machine ({host_label(src_hc)})")

    ws = entry["workspace"]
    src, dst = host_label(src_hc), host_label(dst_hc)
    print(f"luv: handing {ws} from {src} to {dst}")

    if dst_hc is not None:
        preflight_host(dst_hc)
    src_home, src_root = remote_paths(src_hc)
    dst_home, dst_root = remote_paths(dst_hc)
    src_ws, dst_ws = src_root / ws, dst_root / ws

    if ssh_run(src_hc, f"test -d {shlex.quote(str(src_ws))}").returncode != 0:
        die(f"no workspace at {src_ws} on {src}")
    exists = ssh_run(dst_hc, f"test -e {shlex.quote(str(dst_ws))}").returncode == 0
    if exists and not force:
        die(f"{dst_ws} already exists on {dst} — pass --force to replace it")
    before = workspace_git_state(src_hc, src_ws)

    # Stop the agent first: copying a workspace out from under a running one is
    # the only way to get a torn tree. Killing the tmux session SIGHUPs the
    # pane, so launch()'s docker teardown never runs — do it explicitly.
    session = entry.get("session") or tmux_session_name(ws)
    running = ssh_run(
        src_hc, f"tmux has-session -t {shlex.quote(session)} 2>/dev/null"
    ).returncode == 0
    if running:
        print(f"luv: stopping {session} on {src}")
    elif not assume_yes:
        print(f"luv: no tmux session '{session}' on {src} — if an agent is still "
              "running there in another terminal, stop it first.")
        if input("Continue? [y/N]: ").strip().lower() not in ("y", "yes"):
            die("aborted")
    ssh_run(src_hc, f"tmux kill-session -t {shlex.quote(session)} 2>/dev/null; "
                    f"docker compose -p {shlex.quote('luv-' + ws)} "
                    "down -v --remove-orphans 2>/dev/null; true")

    recovery = (f"luv {entry['repo']} {entry['number']} -r"
                + (f" -s {src}" if src_hc else " --local"))
    print(f"luv: copying {ws} to {dst}:{dst_root} (if this fails: {recovery})")
    if exists:
        ssh_run(dst_hc, f"rm -rf {shlex.quote(str(dst_ws))}")
    stream_copy(src_hc, src_root, [ws], dst_hc, dst_root)

    if no_agent_state:
        print("luv: skipping agent state — the agent will start a new conversation")
    else:
        members = agent_state_members(src_hc, entry["agent"] or agent, src_ws, src_home)
        if members:
            print(f"luv: copying agent state ({len(members)} path"
                  f"{'' if len(members) == 1 else 's'})")
            stream_copy(src_hc, src_home, members, dst_hc, dst_home)
            settle_agent_state(dst_hc, entry["agent"] or agent, members,
                               src_ws, dst_ws, dst_home)
        else:
            print(f"luv: warning: no {entry['agent'] or agent} transcript found for "
                  f"{src_ws} — the agent will start a new conversation",
                  file=sys.stderr)

    after = workspace_git_state(dst_hc, dst_ws)
    if after != before:
        die(f"copy does not match the source (HEAD/dirty {before} vs {after}); "
            f"the workspace on {src} is untouched — restart it with: {recovery}")

    if entry.get("id"):
        with session_lock():
            save_sessions([s for s in load_sessions() if s.get("id") != entry["id"]])
    if purge:
        ssh_run(src_hc, f"rm -rf {shlex.quote(str(src_ws))}")
        print(f"luv: removed {src_ws} on {src}")
    else:
        print(f"luv: source kept at {src}:{src_ws} — 'luv --clean' there to reclaim it")

    luv_args = [f"{entry['org']}/{entry['repo']}", str(entry["number"]), "-r"]
    if (entry.get("agent") or agent) == "codex":
        luv_args.append("--codex")
    if entry.get("model"):
        luv_args += ["-m", entry["model"]]
    meta = {"org": entry.get("org"), "repo": entry.get("repo"),
            "agent": entry.get("agent") or agent, "prompt": entry.get("prompt"),
            "model": entry.get("model")}
    if dst_hc is None:
        start_local_session(ws, luv_args, meta, attach)
    else:
        dispatch_remote(dst_hc, luv_args, workspace=ws, meta=meta, detach=not attach)


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
    no_agent_state = "--no-agent-state" in args
    no_attach = "--no-attach" in args
    purge = "--purge" in args
    assume_yes = "-y" in args

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
    to_host = take_value("--to", "a host name")
    from_host = take_value("--from", "a host name")

    if local_mode and (host_flag or identity):
        die("--local cannot be combined with -s or -i")

    args = [a for a in args if a not in ("-n", "-r", "-e", "-f", "--force", "-p", "-nit", "--safe", "--claude", "--codex", "--local", "--no-agent-state", "--no-attach", "--purge", "-y")]

    # Before anything shells out: the clone needs gh and git on PATH as much as
    # the agent needs its credentials, and both come from the same rc. Ahead of
    # collect_luv_env() too, so a LUV_* exported there is one -e can forward.
    apply_shell_env()
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
                (with rm --merged/--dead) skip the confirmation prompt
                (with handover) replace an existing folder on the destination
  --safe        (with --clean -f) only delete folders older than 24h

Handover flags:
  --to HOST     destination machine ('local' for this one)
  --from HOST   source machine (default: wherever luv last saw the session)
  --no-agent-state  move the workspace only; the agent starts a new conversation
  --no-attach   leave the destination session running detached
  --purge       delete the source folder once the copy is verified
  -y            skip the "an agent may still be running" confirmation

Commands:
  luv config                              interactive setup (remote host, SSH key, org)
  luv config set|get|unset <key> [value]  read or write a single setting
  luv config list                         show all settings
  luv --init                              configure default GitHub org only
  luv ls [--host H] [--prune] [--no-pr]   list every live session on every host
  luv ports [<repo> [n]] [--watch [N]]    show detected ports; naming a session
                                          forwards them to localhost
  luv ports --off [<repo> [n]]            drop forwards again
  luv continue [<repo> [number]]          attach to a live session
  luv handover [<repo> [n]] --to HOST     move a session to another machine
  luv rm <session|workspace>...           kill a session and delete its folder
  luv rm --merged [--host H] [-f]         remove every session whose PR is merged
  luv rm --dead [--host H] [-f]           remove workspaces with no live session
  luv [org/]<repo> [prompt...]            create a new PR workspace
  luv [org/]<repo> -b <branch> [prompt]   create a workspace based off <branch>
  luv [org/]<repo> <number> [prompt]      reopen an existing work folder by number
  luv -l <PR URL> [prompt]                clone any GitHub PR by URL into a fresh folder
  luv [org/]<repo> -pr <number> [prompt]  open a GitHub PR by repo + number (reuses its folder)
  luv [org/]<repo> -n                     open shell in latest local clone
  luv [org/]<repo> -r                     resume the selected agent in latest local clone
  luv --clean [-f] [--safe]               delete fully-pushed work folders

Org resolution:
  Explicit org/repo overrides the default. Run 'luv --init' to set a default.
  Config: ~/.luv/config.json

Remote:
  Once 'luv config' has a remote host, every workspace command runs there inside
  a tmux session that survives disconnects. 'luv ls' shows what is running on
  every host — including sessions started from another machine — and
  'luv continue' reattaches. A session that ends badly prints the exact
  'luv continue <repo> <n>' for itself. Use --local for a one-off local run.
  'luv handover' moves a running session — workspace, uncommitted work, and the
  agent's conversation — to another machine, then resumes it there.
  Requires luv, tmux, gh and git on the remote. See docs/remote-sessions.md.

Ports:
  Servers an agent starts on a remote host are found by luv and forwarded to
  this machine on the same port number where it is free. The session you are
  attached to is forwarded automatically as servers come and go, and says so on
  the tmux status line; anything else is opted in with 'luv ports <repo> <n>'.
  'luv ls' shows what was detected without forwarding it.

Naming:
  Workspaces are {repo}-{machine}-{number} and branches luv-{machine}-{number},
  where {machine} is this machine's name (config 'machine', default: hostname).
  It keeps two machines that pick the same number apart. Pre-slug folders and
  branches keep working.

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

    if args[0] == "--where":
        cmd_where(args[1:])
        return

    if args[0] == "--paths":
        cmd_paths()
        return

    if args[0] == "--listening":
        cmd_listening()
        return

    if args[0] == "ls":
        cmd_ls(args[1:], identity)
        return

    if args[0] == "ports":
        cmd_ports(args[1:], identity)
        return

    if args[0] == "continue":
        cmd_continue(args[1:], identity)
        return

    if args[0] == "handover":
        cmd_handover(args[1:], identity=identity, to=to_host, from_host=from_host,
                     agent=agent, force=force, purge=purge,
                     no_agent_state=no_agent_state, attach=not no_attach,
                     assume_yes=assume_yes)
        return

    if args[0] == "rm":
        cmd_rm(args[1:], identity, force=force)
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

        # These forms name an existing workspace, so the remote folder — and
        # with it the tmux session name — can be pinned down before dispatch,
        # which is what lets 'new-session -A' double as attach.
        # -l and -pr also pin down the PR itself, which `luv ls` can't otherwise
        # find: their branch is the PR's head ref, not luv-{machine}-{number}.
        number = pr_hint = None
        if args[0] == "-l" and len(args) > 1:
            m = re.match(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)", args[1])
            if m:
                org_hint, repo_hint = m.group(1), m.group(2)
                pr_hint = int(m.group(3))
                # -l clones into a folder of its own every time, so the name is
                # only knowable once the remote has picked it — leave it on the
                # luv-pending path, which renames the session on arrival. -r is
                # the exception: it reopens the newest folder already there.
                if resume_mode:
                    number = pr_hint
        elif repo_hint and "-pr" in args:
            idx = args.index("-pr")
            if idx + 1 < len(args) and args[idx + 1].isdigit():
                number = pr_hint = int(args[idx + 1])
        elif repo_hint and len(args) > 1 and args[1].isdigit():
            number = int(args[1])

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
        if use_tmux and number is not None and repo_hint:
            workspace = resolve_remote_workspace(host_cfg, org_hint, repo_hint, number)
        prompt_text = remote_prompt(args)
        meta = None
        if use_tmux:
            meta = {"org": org_hint, "repo": repo_hint, "agent": agent,
                    "prompt": prompt_text, "model": model, "pr_hint": pr_hint}
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
        open_pr(org, repo, number, prompt, nav_mode, resume_mode, plan_mode, non_interactive, extra_env=extra_env, model=model, agent=agent, fresh=True)
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

    # 3. Find free local folder. The slug keeps this machine's numbering from
    # colliding with another machine's; the loop handles collisions on this one.
    PRS_DIR.mkdir(parents=True, exist_ok=True)
    while (PRS_DIR / workspace_name(repo, candidate)).exists():
        candidate += 1
    clone_dir = PRS_DIR / workspace_name(repo, candidate)

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
    branch = branch_name(candidate)
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
