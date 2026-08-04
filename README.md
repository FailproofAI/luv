# luv

A CLI that launches Claude Code or Codex agents on GitHub repos with isolated workspaces and optional Docker dev environments.

`luv` clones a repo, creates a branch, and drops you into a Claude session ready to work. When the repo ships a `.luv/settings.json`, it spins up Docker Compose automatically so every command runs in the right environment. Point it at a remote machine and the whole thing runs there in a tmux session you can detach from and reattach to later.

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
# One-time setup: default GitHub org, and optionally a remote machine
luv config

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

# Clone any GitHub PR by URL into a fresh workspace
luv -l https://github.com/org/repo/pull/123

# Open a shell instead of Claude
luv -n my-repo 42

# Resume last Claude session
luv -r my-repo 42

# Clean up fully-merged workspaces
luv --clean

# Tear down a finished session: kill its tmux, delete its workspace
luv rm myrepo-mbp-42
luv rm --merged        # everything whose PR has landed

# See what's running on your remote machine, and reattach
luv ls
luv continue
```

## How it works

1. Clones the repo into `~/prs/{repo}-{machine}-{number}/`
2. Creates a new branch `luv-{machine}-{number}`
3. Configures the selected agent with the workspace's PR conventions
4. Launches Claude with Opus 5 at max effort, or Codex in YOLO mode

All workspaces live under `~/prs/`. The number comes from the repo's GitHub issue counter, and `{machine}` is a short name for the machine that created the workspace — by default the hostname, or whatever you set with `luv config set machine mbp`.

The machine name is what keeps two computers apart. Each one works out the next number from GitHub independently, so your laptop and your box will happily pick the same one; without the slug they would both push a branch called `luv-43`. Folders and branches created before this keep working unchanged.

With a remote host configured, steps 1–4 happen on that machine inside a tmux session instead — see [Remote sessions](#remote-sessions).

### Opening an existing PR

`luv -l <PR URL>` gives you the PR as it is *right now*: a full clone into a
workspace of its own, checked out on the PR's head branch. It never reuses a
folder that happens to be sitting there — a URL you paste is a request to look
at that PR, not at whatever was checked out in `myrepo-mbp-123` last week. Run
it twice on the same PR and you get `myrepo-mbp-123` and `myrepo-mbp-123_2`,
which are independent workspaces you can run side by side. When the PR comes
from a fork, the base repo is added as `upstream` and fetched too, so the branch
you need to diff or rebase against is there.

`-r` is the exception: resuming means picking up a conversation, and a
conversation lives in the folder it was held in, so `luv -l <URL> -r` reopens the
newest workspace for that PR instead of cloning another. `luv <repo> -pr <N>`
also keeps reusing its workspace — it names a workspace, where a URL names a PR.

## Commands

| Command | Description |
|---------|-------------|
| `luv config` | Interactive setup: remote host, SSH key, default org |
| `luv config set\|get\|unset <key> [value]` | Read or write a single setting |
| `luv config list` | Show all settings |
| `luv --init` | Configure default GitHub org only |
| `luv ls [--host H] [--prune] [--no-pr]` | List every live session on every host, with each one's PR link |
| `luv continue [<repo> [number]]` | Attach to a live session |
| `luv handover [<repo> [n]] --to HOST` | Move a session to another machine and resume it there |
| `luv rm <session\|workspace>...` | Kill a session and delete its workspace on its host |
| `luv rm --merged [--host H] [-f]` | Remove every session whose PR is merged |
| `luv rm --dead [--host H] [-f]` | Remove workspaces with no live session |
| `luv [org/]<repo> [prompt...]` | Create a new workspace and launch Claude (default) |
| `luv --codex [org/]<repo> [prompt...]` | Create a workspace and launch Codex in YOLO mode |
| `luv [org/]<repo> -b <branch> [prompt...]` | Create a workspace based off `<branch>` instead of the default |
| `luv [org/]<repo> <number> [prompt]` | Reopen an existing workspace |
| `luv -l <PR URL> [prompt]` | Clone any GitHub PR by URL into a fresh workspace |
| `luv [org/]<repo> -pr <number> [prompt]` | Open a PR by repo + number, reusing its workspace |
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
| `-m MODEL` | Model to use; Claude defaults to `claude-opus-5`, while Codex uses its configured CLI default |
| `-b BRANCH` | Base a new workspace off `BRANCH` (clone + branch from it); recorded in `git config luv.base` so the PR can target it |
| `-e` | Env: pass `LUV_*` environment variables (with prefix stripped) into the session |
| `-s HOST` | Run on `HOST` over SSH, overriding the configured remote host |
| `-i PATH` | SSH identity file to use for this invocation |
| `--local` | Force local execution even when a remote host is configured |
| `-f`, `--force` | Skip safety checks (with `--clean`); replace an existing destination folder (with `handover`) |
| `--safe` | With `--clean -f`, only delete workspaces older than 24h (mtime) |

Handover flags:

| Flag | Description |
|------|-------------|
| `--to HOST` | Destination machine; `local` for the one you're on |
| `--from HOST` | Source machine, when luv can't tell from its session registry |
| `--no-agent-state` | Move the workspace only; the agent starts a new conversation |
| `--no-attach` | Leave the destination session running detached |
| `--purge` | Delete the source folder once the copy is verified |
| `-y` | Skip the "an agent may still be running" confirmation |

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
3. Starts `docker compose up -d --build` with a unique project name (`luv-{repo}-{machine}-{number}`) for network/volume isolation
4. Verifies the `dev-environment` service is running
5. Runs the selected agent inside the container via `docker compose exec`
6. The repo is volume-mounted, so all file changes and git commits are visible on the host
7. On exit (including Ctrl-C), tears down the environment with `docker compose down -v`

Docker mode works with all flags: `-n` opens a bash shell in the container, `-r` resumes a Claude session in the container.

## Remote sessions

Point `luv` at another machine and every workspace command runs there, inside a tmux session that survives disconnects.

```bash
luv config                          # set the remote host, SSH key, and org
luv myrepo "fix the flaky test"     # clones and launches Claude on the remote
                                    # ...close the laptop, go home...
luv ls                              # see what's still running
luv continue                        # reattach exactly where you left off
```

`luv` re-invokes itself on the remote over SSH, so every flag works there unchanged. The remote needs `luv`, `tmux`, `gh`, and `git` on its `PATH`.

`luv ls` lists what is running on every host it knows about, not only the sessions this machine started — a session dispatched from your laptop shows up on your desktop, and `luv continue` and `luv rm` reach it from either.

Use `--local` for a one-off local run, `-s HOST` to target a different machine, and `-i PATH` for a different SSH key.

### Handing a session to another machine

Start something on your laptop, then move it — workspace, uncommitted work, and the agent's conversation — to a machine that won't go to sleep:

```bash
luv --local myrepo "start something"   # working locally
luv handover myrepo 43 --to box        # resumes mid-conversation on box
luv handover myrepo 43 --to local      # ...and back again later
```

The whole folder crosses over, including untracked and gitignored files, so a workspace that ran before the move still runs after it. The agent's transcript moves too, so it picks up where it left off rather than starting fresh. Bytes are relayed through the machine you run the command on, so the two ends never need SSH access to each other.

The source folder is left on disk (`--purge` deletes it), and the workspace keeps the name it was created with.

Full guide, including how to prepare a fresh Ubuntu box and set up SSH keys: **[docs/remote-sessions.md](docs/remote-sessions.md)**.

## Workspace cleanup

`luv --clean` scans `~/prs/` and safely removes workspaces that are fully pushed. It checks:

- Working tree is clean (no uncommitted changes)
- No unpushed commits
- If the remote branch is gone, verifies the PR was merged and local HEAD matches

Use `luv --clean -f` to skip all safety checks and delete everything. Add `--safe` (i.e. `luv --clean --safe -f`) to restrict force-delete to workspaces whose folder mtime is older than 24 hours, leaving recently-touched workspaces alone.

Workspaces with a live tmux session are skipped unless you pass `-f`.

### Removing a specific session

`luv --clean` is a garbage collector: it decides for itself what is safe to
delete, and never touches a workspace with a live session. `luv rm` is the
opposite — you name what goes, and it goes, on whichever host it lives on.

```bash
luv rm myrepo-mbp-42      # kill its tmux session, delete the folder on its host
luv rm luv-myrepo-mbp-42  # the session name works too
luv rm --merged           # every session whose PR has been merged
luv rm --dead             # workspaces left behind by sessions that already exited
luv rm --dead --host box  # scope either selector to one machine
```

A named target is taken as intent and removed without further checks —
including any uncommitted work in it. The `--merged` and `--dead` selectors
list what they matched and ask first; `-f` skips that prompt.

`--dead` is the counterpart to `luv ls`: reconciliation drops a session from the
registry as soon as its tmux session exits, but the clone it was working in
stays on the remote disk. Those orphans are invisible to `luv ls` and are
usually what is actually filling up the box.

Only folders ending in `-{N}` are ever eligible — `{repo}-{machine}-{N}`, a
`luv -l` copy `{repo}-{machine}-{N}_2`, and pre-slug `{repo}-{N}` alike.
Anything else in the workspace root is left alone, and a target that doesn't
resolve to one is an error rather than an `rm -rf`.

## Configuration

Run `luv config` for interactive setup, or `luv --init` to set just your default GitHub org. Both save to `~/.luv/config.json`:

```json
{
  "org": "exosphere",
  "prs_dir": "~/prs",
  "remote": {
    "host": "box",
    "identity_file": "~/.ssh/id_ed25519",
    "dir": "~/prs"
  }
}
```

You can also pass `org/repo` inline to override the default for any command (e.g., `luv other-org/my-repo`).

Every key, with defaults and resolution order: **[docs/configuration.md](docs/configuration.md)**. The session registry that backs `luv ls`: **[docs/sessions.md](docs/sessions.md)**.

## License

MIT
