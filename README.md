# luv

A CLI that launches Claude Code or Codex agents on GitHub repos with isolated workspaces and optional Docker dev environments.

`luv` clones a repo, creates a branch, and drops you into a Claude session ready to work. When the repo ships a `.luv/settings.json`, it spins up Docker Compose automatically so every command runs in the right environment.

## Install

```bash
# With uv (recommended)
uv tool install luv-cli

# With pip
pip install luv-cli
```

**Requirements:** [GitHub CLI](https://cli.github.com/) (`gh`) plus either Claude Code or Codex must be installed and authenticated.

## Quick start

```bash
# Configure your default GitHub org (one-time setup)
luv --init

# Create a new workspace and launch Claude
luv my-repo "add user authentication"

# Launch Codex in YOLO mode instead
luv --codex my-repo "add user authentication"

# Select Claude explicitly (Claude remains the default)
luv --claude my-repo "add user authentication"

# Base a new workspace off a non-default branch
luv my-repo -b develop "add user authentication"

# Use a different org inline
luv other-org/my-repo "fix the bug"

# Reopen workspace #42
luv my-repo 42

# Open any GitHub PR by URL
luv -l https://github.com/org/repo/pull/123

# Open a shell instead of Claude
luv -n my-repo 42

# Resume last Claude session
luv -r my-repo 42

# Clean up fully-merged workspaces
luv --clean
```

## How it works

1. Clones the repo into `~/prs/{repo}-{number}/`
2. Creates a new branch `luv-{number}`
3. Configures the selected agent with the workspace's PR conventions
4. Launches Claude with Opus 4.8 at max effort, or Codex in YOLO mode

All workspaces live under `~/prs/`. The number comes from the repo's GitHub issue counter to avoid collisions.

## Commands

| Command | Description |
|---------|-------------|
| `luv --init` | Configure default GitHub org |
| `luv [org/]<repo> [prompt...]` | Create a new workspace and launch Claude (default) |
| `luv --codex [org/]<repo> [prompt...]` | Create a workspace and launch Codex in YOLO mode |
| `luv [org/]<repo> -b <branch> [prompt...]` | Create a workspace based off `<branch>` instead of the default |
| `luv [org/]<repo> <number> [prompt]` | Reopen an existing workspace |
| `luv -l <PR URL> [prompt]` | Open any GitHub PR by URL |
| `luv [org/]<repo> -pr <number> [prompt]` | Open a PR by repo + number |
| `luv --clean` | Delete workspaces where the branch is fully pushed/merged |
| `luv --clean -f` | Force delete all workspaces |
| `luv --clean --safe -f` | Force delete only workspaces older than 24h |

### Flags

| Flag | Description |
|------|-------------|
| `--claude` | Launch Claude Code (default) |
| `--codex` | Launch Codex with approvals and sandboxing bypassed (YOLO mode) |
| `-n` | Navigate: open a shell instead of Claude |
| `-r` | Resume: resume the selected agent's last session |
| `-p` | Launch Claude in plan permission mode (default: `bypassPermissions`) |
| `-nit` | Non-interactive: run the selected agent and exit (no REPL); Claude streams `stream-json` events to stdout |
| `-m MODEL` | Model to use; Claude defaults to `claude-opus-4-8`, while Codex uses its configured CLI default |
| `-b BRANCH` | Base a new workspace off `BRANCH` (clone + branch from it); recorded in `git config luv.base` so the PR can target it |
| `-e` | Env: pass `LUV_*` environment variables (with prefix stripped) into the session |
| `-f`, `--force` | Skip safety checks (with `--clean`) |
| `--safe` | With `--clean -f`, only delete workspaces older than 24h (mtime) |

## Docker dev environments

If a repo contains `.luv/settings.json` with a `compose_file` key, `luv` automatically starts a Docker Compose environment and runs Claude inside the `dev-environment` container.

### Setup

**1. Create `.luv/settings.json` in your repo:**

```json
{
  "compose_file": ".luv/docker-compose.yml"
}
```

The `compose_file` path is relative to the repo root.

**2. Create the Docker Compose file:**

```yaml
services:
  dev-environment:
    image: your-org/dev-env:latest
    volumes:
      - .:/workspace
    working_dir: /workspace
    stdin_open: true
    tty: true
    depends_on:
      - postgres

  postgres:
    image: postgres:16
    environment:
      POSTGRES_PASSWORD: dev
```

The `dev-environment` service **must** have the selected agent CLI (`claude` or `codex`) installed in its image.

### How Docker mode works

1. Detects `.luv/settings.json` with `compose_file` key
2. Tears down any stale environment from a previous run
3. Starts `docker compose up -d --build` with a unique project name (`luv-{repo}-{number}`) for network/volume isolation
4. Verifies the `dev-environment` service is running
5. Runs the selected agent inside the container via `docker compose exec`
6. The repo is volume-mounted, so all file changes and git commits are visible on the host
7. On exit (including Ctrl-C), tears down the environment with `docker compose down -v`

Docker mode works with all flags: `-n` opens a bash shell in the container, `-r` resumes a Claude session in the container.

## Workspace cleanup

`luv --clean` scans `~/prs/` and safely removes workspaces that are fully pushed. It checks:

- Working tree is clean (no uncommitted changes)
- No unpushed commits
- If the remote branch is gone, verifies the PR was merged and local HEAD matches

Use `luv --clean -f` to skip all safety checks and delete everything. Add `--safe` (i.e. `luv --clean --safe -f`) to restrict force-delete to workspaces whose folder mtime is older than 24 hours, leaving recently-touched workspaces alone.

## Configuration

Run `luv --init` to set your default GitHub org. This saves to `~/.luv/config.json`.

You can also pass `org/repo` inline to override the default for any command (e.g., `luv other-org/my-repo`).

## License

MIT
