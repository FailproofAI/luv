import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LUV_DIR = Path.home() / ".luv"
CONFIG_FILE = LUV_DIR / "config.json"
PRS_DIR = Path.home() / "prs"
CLAUDE_JSON = Path.home() / ".claude.json"
CLAUDE_SETTINGS_JSON = Path.home() / ".claude" / "settings.json"

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


def run(cmd: list[str], *, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, cwd=cwd)


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


def ensure_pr_rules() -> None:
    claude_dir = Path.home() / ".claude"
    claude_md = claude_dir / "CLAUDE.md"
    claude_dir.mkdir(parents=True, exist_ok=True)
    existing = claude_md.read_text() if claude_md.exists() else ""
    if "# Pull Request Management" not in existing:
        with claude_md.open("a") as f:
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


def cmd_init() -> None:
    """Interactive setup: choose a default GitHub org."""
    if not sys.stdin.isatty():
        die("--init requires an interactive terminal")

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

    config = load_config()
    config["org"] = selected
    save_config(config)
    print(f"luv: default org set to '{selected}'. Saved to ~/.luv/config.json")


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
           model: str = "claude-opus-4-7") -> None:
    """Trust, chdir, and exec claude --resume — replacing this process."""
    extra_env = extra_env or {}
    trust_project(clone_dir)
    os.chdir(str(clone_dir))
    settings = load_luv_settings(clone_dir)
    compose_file = (settings or {}).get("compose_file")

    if compose_file:
        project = docker_project_name(clone_dir)
        start_docker(clone_dir, compose_file, project)
        try:
            base = docker_compose_base(clone_dir, compose_file, project)
            r = subprocess.run(base + ["exec", "-it"] + docker_env_flags(extra_env) + ["dev-environment",
                                       "claude", "--dangerously-skip-permissions",
                                       "--model", model,
                                       "--effort", "max", "--resume",
                                       "--remote-control",
                                       "--remote-control-session-name-prefix", clone_dir.name])
            sys.exit(r.returncode)
        finally:
            stop_docker(clone_dir, compose_file, project)
    else:
        claude_bin = shutil.which("claude")
        if not claude_bin:
            die("'claude' not found in PATH")
        os.environ.update(extra_env)
        os.execv(claude_bin, [claude_bin, "--dangerously-skip-permissions",
                              "--model", model, "--effort", "max", "--resume",
                              "--remote-control",
                              "--remote-control-session-name-prefix", clone_dir.name])


def launch(clone_dir: Path, prompt: str | None, plan_mode: bool = False,
           non_interactive: bool = False, extra_env: dict[str, str] | None = None,
           model: str = "claude-opus-4-7") -> None:
    """Trust, resolve claude, chdir, and exec — replacing this process."""
    extra_env = extra_env or {}
    trust_project(clone_dir)
    os.chdir(str(clone_dir))
    settings = load_luv_settings(clone_dir)
    compose_file = (settings or {}).get("compose_file")

    common_flags = ["--dangerously-skip-permissions",
                    "--model", model,
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

    if compose_file:
        project = docker_project_name(clone_dir)
        start_docker(clone_dir, compose_file, project)
        try:
            base = docker_compose_base(clone_dir, compose_file, project)
            claude_cmd = ["claude"] + common_flags + mode_flags + initial_args
            r = subprocess.run(base + ["exec", "-it"] + docker_env_flags(extra_env) + ["dev-environment"] + claude_cmd)
            sys.exit(r.returncode)
        finally:
            stop_docker(clone_dir, compose_file, project)
    else:
        claude_bin = shutil.which("claude")
        if not claude_bin:
            die("'claude' not found in PATH")
        os.environ.update(extra_env)
        os.execv(claude_bin, [claude_bin] + common_flags + mode_flags + initial_args)


SAFE_AGE_SECONDS = 24 * 3600


def cmd_clean(force: bool = False, safe: bool = False) -> None:
    """Scan ~/prs/ and delete fully-pushed, clean work folders."""
    if not PRS_DIR.exists():
        print("luv: nothing to clean (~/prs/ does not exist)")
        return

    cleaned: list[str] = []
    skipped: list[tuple[str, str]] = []
    now = time.time()

    for entry in sorted(PRS_DIR.iterdir()):
        if not entry.is_dir():
            continue

        parts = entry.name.rsplit("-", 1)
        if len(parts) != 2 or not parts[1].isdigit():
            continue  # doesn't match {repo}-{number} — skip silently

        if force:
            if safe and (now - entry.stat().st_mtime) < SAFE_AGE_SECONDS:
                skipped.append((entry.name, "younger than 24h (--safe)"))
                continue
            shutil.rmtree(entry)
            cleaned.append(entry.name)
            continue

        number_str = parts[1]
        branch = f"luv-{number_str}"
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
            shutil.rmtree(entry)
            cleaned.append(entry.name)
            continue

        # 3. No unpushed commits (branch still exists on remote)
        r = run(["git", "rev-list", f"origin/{branch}..HEAD", "--count"], cwd=cwd)
        if r.returncode != 0 or r.stdout.strip() != "0":
            skipped.append((entry.name, "unpushed commits"))
            continue

        shutil.rmtree(entry)
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


def open_existing(org: str, repo: str, number: int, prompt: str | None, nav_mode: bool = False, resume_mode: bool = False, plan_mode: bool = False, non_interactive: bool = False, extra_env: dict[str, str] | None = None, model: str = "claude-opus-4-7") -> None:
    """Open an existing work folder or remote branch by number."""
    extra_env = extra_env or {}
    clone_dir = PRS_DIR / f"{repo}-{number}"

    # 1. Local folder takes priority
    if clone_dir.exists():
        print(f"luv: opening existing folder {clone_dir.name}")
        ensure_pr_rules()
        if nav_mode:
            navigate(clone_dir, extra_env=extra_env)
        elif resume_mode:
            resume(clone_dir, extra_env=extra_env, model=model)
        else:
            launch(clone_dir, prompt, plan_mode=plan_mode, non_interactive=non_interactive, extra_env=extra_env, model=model)
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
    ensure_pr_rules()
    if nav_mode:
        navigate(clone_dir, extra_env=extra_env)
    elif resume_mode:
        resume(clone_dir, extra_env=extra_env, model=model)
    else:
        launch(clone_dir, prompt, plan_mode=plan_mode, non_interactive=non_interactive, extra_env=extra_env, model=model)


def open_pr(org: str, repo: str, number: int, prompt: str | None, nav_mode: bool = False, resume_mode: bool = False, plan_mode: bool = False, non_interactive: bool = False, extra_env: dict[str, str] | None = None, model: str = "claude-opus-4-7") -> None:
    """Open any GitHub PR by org/repo/number, cloning if needed."""
    extra_env = extra_env or {}
    clone_dir = PRS_DIR / f"{repo}-{number}"

    if clone_dir.exists():
        print(f"luv: opening existing folder {clone_dir.name}")
        ensure_pr_rules()
        if nav_mode:
            navigate(clone_dir, extra_env=extra_env)
        elif resume_mode:
            resume(clone_dir, extra_env=extra_env, model=model)
        else:
            launch(clone_dir, prompt, plan_mode=plan_mode, non_interactive=non_interactive, extra_env=extra_env, model=model)
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
    ensure_pr_rules()
    if nav_mode:
        navigate(clone_dir, extra_env=extra_env)
    elif resume_mode:
        resume(clone_dir, extra_env=extra_env, model=model)
    else:
        launch(clone_dir, prompt, plan_mode=plan_mode, non_interactive=non_interactive, extra_env=extra_env, model=model)


def main() -> None:
    args = sys.argv[1:]

    nav_mode = "-n" in args
    resume_mode = "-r" in args
    plan_mode = "-p" in args
    non_interactive = "-nit" in args
    force = "-f" in args or "--force" in args
    safe = "--safe" in args
    env_mode = "-e" in args

    # -m takes a value, so extract it before the boolean-flag strip below
    model = "claude-opus-4-7"
    if args.count("-m") > 1:
        die("-m may only be provided once")
    if "-m" in args:
        idx = args.index("-m")
        if idx + 1 >= len(args):
            die("-m requires a model name")
        model = args[idx + 1]
        args = args[:idx] + args[idx + 2:]

    args = [a for a in args if a not in ("-n", "-r", "-e", "-f", "--force", "-p", "-nit", "--safe")]
    extra_env = collect_luv_env() if env_mode else {}

    if not args or args[0] in ("-h", "--help"):
        print("""\
Usage: luv [flags] <command>

Flags:
  -n            navigate: open a shell in the work folder instead of launching Claude
  -r            resume: resume the last Claude session in the work folder
  -p            launch Claude in plan permission mode (default: bypassPermissions)
  -nit          non-interactive: run claude -p <prompt> and exit (no REPL)
  -m MODEL      claude model to use (default: claude-opus-4-7)
  -e            env: pass LUV_* environment variables (with prefix stripped) into the session
  -f, --force   (with --clean) skip safety checks and delete all work folders
  --safe        (with --clean -f) only delete folders older than 24h

Commands:
  luv --init                              configure default GitHub org
  luv [org/]<repo> [prompt...]            create a new PR workspace
  luv [org/]<repo> <number> [prompt]      reopen an existing work folder by number
  luv -l <PR URL> [prompt]                open any GitHub PR by URL
  luv [org/]<repo> -pr <number> [prompt]  open a GitHub PR by repo + number
  luv [org/]<repo> -n                     open shell in latest local clone
  luv [org/]<repo> -r                     resume Claude in latest local clone
  luv --clean [-f] [--safe]               delete fully-pushed work folders

Org resolution:
  Explicit org/repo overrides the default. Run 'luv --init' to set a default.
  Config: ~/.luv/config.json

Docker:
  If the repo contains .luv/settings.json with a "compose_file" key,
  luv starts a Docker Compose environment and runs Claude inside the
  "dev-environment" service. Torn down automatically on exit.""")
        sys.exit(0)

    if safe and (args[0] != "--clean" or not force):
        die("--safe only works with --clean -f")

    if args[0] == "--clean":
        cmd_clean(force=force, safe=safe)
        return

    if args[0] == "--init":
        cmd_init()
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
        open_pr(org, repo, number, prompt, nav_mode, resume_mode, plan_mode, non_interactive, extra_env=extra_env, model=model)
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
        open_pr(resolve_org(explicit_org), repo, number, prompt, nav_mode, resume_mode, plan_mode, non_interactive, extra_env=extra_env, model=model)
        return

    # Detect optional numeric second argument
    if len(args) > 1 and args[1].isdigit():
        number = int(args[1])
        prompt = " ".join(args[2:]) or None
        open_existing(resolve_org(explicit_org), repo, number, prompt, nav_mode, resume_mode, plan_mode, non_interactive, extra_env=extra_env, model=model)
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
            resume(clone_dir, extra_env=extra_env, model=model)
        return

    # 1. Verify repo exists
    r = run(["gh", "api", f"repos/{org}/{repo}"])
    if r.returncode != 0:
        die(f"repo '{org}/{repo}' not found or gh auth failed.\n{r.stderr.strip()}")

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

    # 4. Clone
    clone_url = f"https://github.com/{org}/{repo}"
    print(f"luv: cloning {clone_url} -> {clone_dir}")
    r = subprocess.run(["git", "clone", clone_url, str(clone_dir)])
    if r.returncode != 0:
        die(f"git clone failed (exit {r.returncode})")

    # 5. Create branch
    branch = f"luv-{candidate}"
    print(f"luv: creating branch {branch}")
    r = subprocess.run(["git", "checkout", "-b", branch], cwd=str(clone_dir))
    if r.returncode != 0:
        die(f"git checkout -b failed (exit {r.returncode})")

    # 6. Ensure PR rules in ~/.claude/CLAUDE.md and bypass-permissions default
    ensure_pr_rules()
    ensure_default_permission_mode()

    print(f"luv: ready — {clone_dir.name}, branch {branch}")

    # 7. Launch claude, resume session, or open shell (replace this process)
    if nav_mode:
        navigate(clone_dir, extra_env=extra_env)
    elif resume_mode:
        resume(clone_dir, extra_env=extra_env, model=model)
    else:
        launch(clone_dir, prompt, plan_mode=plan_mode, non_interactive=non_interactive, extra_env=extra_env, model=model)
