# Remote sessions

Run luv workspaces on another machine, inside tmux sessions that survive
disconnects. Close your laptop mid-task, reopen it later, and `luv continue`
puts you back exactly where you were — agent still running, Docker environment
still up.

- [How it works](#how-it-works)
- [Preparing a new remote machine (Ubuntu)](#preparing-a-new-remote-machine-ubuntu)
- [Configuring luv](#configuring-luv)
- [Daily use](#daily-use)
- [Handing a session to another machine](#handing-a-session-to-another-machine)
- [Session lifecycle](#session-lifecycle)
- [Session naming and identity](#session-naming-and-identity)
- [What runs where](#what-runs-where)
- [Docker repos](#docker-repos)
- [Troubleshooting](#troubleshooting)

## How it works

luv on your laptop is a thin dispatcher. It doesn't reimplement anything for
remote use — it re-invokes **luv itself** on the remote machine over SSH, inside
a tmux session:

```
LAPTOP                                REMOTE MACHINE
luv myrepo "fix the bug"
  │
  └─ ssh -t box 'bash -lc "…"'  ────►  tmux new-session -A -s luv-pending-3f9a
                                         └─ luv exosphere/myrepo "fix the bug"
                                              ├─ gh api        → next number = 42
                                              ├─ git clone     → ~/prs/myrepo-box-42
                                              ├─ git checkout -b luv-box-42
                                              ├─ tmux rename-session → luv-myrepo-box-42
                                              └─ exec claude …
                                       (session keeps running after you disconnect)
```

Two things are worth knowing because they explain most of the behaviour:

**tmux wraps the whole remote invocation, not just the agent.** The clone,
`docker compose up`, and the agent all live inside the pane from the first
moment. A connection that drops during a slow clone or image build loses
nothing, and container teardown stays tied to the agent exiting rather than to
your SSH client going away.

**The remote stamps identity onto the session.** Your laptop can't know the
workspace number ahead of time — it comes from `gh api` on the remote — so it
records a random token and the remote writes that token onto the tmux session as
a `@luv_id` option. That's how `luv ls` matches your local registry to live
sessions even after the session is renamed. See [sessions.md](sessions.md).

## Preparing a new remote machine (Ubuntu)

The remote needs four things on `PATH`: **git**, **tmux**, **gh**, and **luv**.
Run these on the remote machine.

### 1. Base packages

```bash
sudo apt update
sudo apt install -y git tmux curl
```

### 2. GitHub CLI

`gh` isn't in Ubuntu's default repos, so add GitHub's:

```bash
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
  | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null
sudo apt update && sudo apt install -y gh
```

Then authenticate. luv uses `gh` to number workspaces and to clone, so this must
be done **as the user you SSH in as**, not as root:

```bash
gh auth login          # choose HTTPS, and let it set up git credentials
gh auth status         # verify
```

For an unattended machine, a token works too:

```bash
echo "ghp_yourtoken" | gh auth login --with-token
```

### 3. luv itself

```bash
# with uv (recommended)
curl -LsSf https://astral.sh/uv/install.sh | sh
uv tool install luv-cli

# or with pip
pip install --user luv-cli
```

Both install into `~/.local/bin`. Confirm a **login** shell finds it, since
that's what luv uses over SSH:

```bash
bash -lc 'command -v luv tmux gh git'
```

If `luv` is missing from that output, `~/.local/bin` isn't on your login `PATH`.
Add it:

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.profile
```

(Ubuntu's default `~/.profile` already adds `~/.local/bin`, but only if the
directory existed when the shell started — so a fresh install often needs one
logout/login, or the line above.)

Alternatively, skip the `PATH` problem entirely by telling luv the full path:

```bash
# on your laptop
luv config set remote.luv_bin /home/youruser/.local/bin/luv
```

### 4. The agent

Install whichever agent you launch — Claude Code and/or Codex — on the remote,
and sign in there once:

```bash
npm install -g @anthropic-ai/claude-code
claude          # sign in, then exit
```

### 5. SSH access with keys

Run this part **on your laptop**.

Create a key if you don't have one. Ed25519 is the right default:

```bash
ssh-keygen -t ed25519 -C "laptop -> luv remote"
# press Enter for the default path (~/.ssh/id_ed25519)
```

Use a passphrase, and add it to your agent so luv isn't prompted on every
connection:

```bash
eval "$(ssh-agent -s)"
ssh-add ~/.ssh/id_ed25519
```

Copy the public key to the remote:

```bash
ssh-copy-id youruser@203.0.113.10
```

If `ssh-copy-id` isn't available, do it by hand:

```bash
ssh youruser@203.0.113.10 'mkdir -p ~/.ssh && chmod 700 ~/.ssh'
cat ~/.ssh/id_ed25519.pub | ssh youruser@203.0.113.10 \
  'cat >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys'
```

Permissions matter — `sshd` refuses keys on a world-writable `~/.ssh` or
`authorized_keys`. Verify login works without a password:

```bash
ssh youruser@203.0.113.10 true && echo OK
```

Give the host a short alias in `~/.ssh/config`. This is the **preferred** place
for per-host SSH settings; luv's `identity_file` and `port` options exist for
convenience, not to replace it:

```
Host box
    HostName 203.0.113.10
    User youruser
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 30
    ServerAliveCountMax 6
```

`ServerAliveInterval` is worth setting: without it a NAT or Wi-Fi hiccup leaves
a half-dead SSH client hanging instead of dropping cleanly (your tmux session is
unaffected either way, but the terminal feels stuck).

Now `ssh box` should just work, and you can use `box` as luv's host name.

### 6. Optional: keep the machine from sleeping

A laptop-as-remote will suspend and kill your sessions. On a server this is
usually already correct, but if in doubt:

```bash
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
```

## Configuring luv

On your laptop:

```bash
luv config
```

The wizard asks for this machine's name, the host, the SSH identity file, and
the remote workspace directory, then offers to set your default GitHub org. It
finishes by checking the remote for `tmux`, `luv`, `gh`, and `git` and telling
you exactly what's missing.

Non-interactively:

```bash
luv config set machine mbp
luv config set remote.host box
luv config set remote.identity_file ~/.ssh/id_ed25519
luv config list
```

Setting `machine` on each machine you use is worth the ten seconds: it is the
label that ends up in every workspace folder and branch name, and the hostname
default is rarely the nicest one.

Full key reference: [configuration.md](configuration.md).

## Daily use

Once a host is configured, **every workspace command runs there**. Nothing else
about how you use luv changes:

```bash
luv myrepo "fix the flaky test"     # new workspace on the remote
luv myrepo 42 "keep going"          # reopen workspace 42 there
luv ls                              # what's running, on every host, whoever started it
luv continue                        # attach to a session
luv continue myrepo                 # attach to the newest myrepo session
luv continue myrepo 42              # attach to a specific workspace
```

Escape hatches:

```bash
luv --local myrepo "quick thing"    # ignore the remote, run here
luv -s gpu-box ml "train it"        # use a different host this once
luv -i ~/.ssh/other_key myrepo      # use a different key this once
```

Detach from a session with `Ctrl-b d` (tmux's default prefix). The agent keeps
running.

## Handing a session to another machine

`luv handover` relocates a running workspace. The usual direction is laptop →
box: you start something locally, then decide it should keep running somewhere
that doesn't sleep.

```bash
luv --local myrepo "start something"   # → ~/prs/myrepo-mbp-43, branch luv-mbp-43
luv handover myrepo 43 --to box        # attaches to it, now running on box
luv handover myrepo 43 --to local      # and back down again later
```

```
                LAPTOP (the one you run the command on)
                  │
  ssh box tar -c ─┤─ ssh gpu tar -x        ← bytes are relayed through here, so
                  │                          box and gpu need no keys for each other
   BOX                                    GPU-BOX
   tmux kill-session luv-myrepo-mbp-43
   docker compose -p … down
   ~/prs/myrepo-mbp-43   ───────────────► ~/prs/myrepo-mbp-43     (whole folder)
   ~/.claude/projects/-home-u-prs-…  ───► ~/.claude/projects/…    (transcript)
   (folder left in place)                 tmux new-session -A -s luv-myrepo-mbp-43
                                            └─ luv exo/myrepo 43 -r → claude --resume
```

**Everything crosses over.** The folder is copied byte for byte — `.git`, staged
and unstaged edits, untracked files, and gitignored ones like `.env` and
`node_modules`. A workspace that ran before the move still runs after it.

**So does the conversation.** Claude's transcript
(`~/.claude/projects/<path-slug>/*.jsonl`) and Codex's rollouts
(`~/.codex/sessions/**/rollout-*.jsonl`) travel with the workspace, and the
absolute path recorded inside them is rewritten for the new machine — the two
rarely agree on it. The destination then starts with `-r`, so `claude --resume`
or `codex resume --last` continues the thread rather than opening a new one.
This part depends on each agent's on-disk layout, so luv says what it found and
carries on with a fresh conversation if it finds nothing; `--no-agent-state`
skips it deliberately.

**The agent is briefly down.** luv checks everything it can first — the
destination is reachable, has luv, and has no folder in the way — then stops the
agent, because copying a workspace out from under a running one is the only way
to get a torn tree. Killing the tmux session SIGHUPs the pane, so luv also tears
the Docker environment down explicitly rather than relying on the agent's exit.

**Nothing is deleted.** The source folder stays on disk, so a failed transfer
costs nothing: luv prints the command to restart the session where it was. Use
`--purge` to delete it once the copy verifies, or `luv --clean` on that machine
later.

**The name doesn't change.** `myrepo-mbp-43` stays `myrepo-mbp-43` on the box —
the slug records where a workspace came from, and its branch may already be
pushed. That is also what makes the move safe: a workspace created on the box
would be `myrepo-box-43`, so the two can sit side by side.

Handover works on a workspace whose agent has already exited, too — name the
machine with `--from` and luv will move the folder and start it fresh.

| Flag | |
|---|---|
| `--to HOST` | Destination; `local` for the machine you're on. Required. |
| `--from HOST` | Source, when luv's registry doesn't already know |
| `--no-agent-state` | Move the workspace only |
| `--no-attach` | Leave the new session detached |
| `--purge` | Delete the source folder after the copy verifies |
| `-f` | Replace an existing folder on the destination |
| `-y` | Skip the "an agent may still be running" confirmation |

## Session lifecycle

| Step | What happens |
|---|---|
| `luv myrepo "…"` | Laptop records a registry entry, opens SSH, creates the tmux session, remote luv clones and launches the agent |
| `Ctrl-b d` or lost connection | tmux session keeps running; agent unaffected; Docker containers stay up |
| `luv ls` | Laptop queries every known host's tmux and refreshes the registry, adopting sessions another machine started |
| `luv continue` | Reattaches; other clients are detached so the pane isn't size-clamped |
| Agent exits | The pane's command ends, tmux session disappears, Docker environment is torn down, `luv ls` prunes the entry on its next run |

## Session naming and identity

Workspace folders are `{repo}-{machine}-{number}` and branches
`luv-{machine}-{number}`, where `{machine}` names whichever machine created the
workspace (config key `machine`, defaulting to the hostname). Each machine works
out the next number from GitHub's issue counter on its own, so without that slug
a laptop and a box would routinely pick the same one and push the same branch.
Folders and branches created before slugs existed keep working — luv looks for
its own name first, then any other machine's, then the plain `{repo}-{number}`.

`luv -l <PR URL>` clones into a folder of its own every time, so a second run on
the same PR gets `{repo}-{machine}-{number}_2`, a third `_3`. The suffix is part
of the number's segment, so those folders are still workspace `{number}`
everywhere it matters — `luv ls`, `luv rm`, `--clean`, handover. Where a number
has several copies, the newest wins: that is the one you just made.

Sessions are named after the workspace folder: `~/prs/myrepo-box-42` becomes
`luv-myrepo-box-42`. tmux forbids `.` and `:` in session names, so a repo like
`foo.js` becomes `luv-foo_js-box-42`.

When the workspace is already known — reopening by number, `-pr`, or `-l -r` —
the laptop pins the session name down before dispatching, so `tmux new-session
-A` doubles as "attach if it's already running". It can't compute that name,
though, since the slug belongs to the machine holding the folder: it takes it
from the session registry, or asks the host directly, and falls back to the
`luv-pending-<id>` rename-on-arrival path if the host doesn't answer.

When it isn't known — a brand new workspace, plain `luv -l` (whose folder the
remote only picks once it sees what is already there), or bare `-n`/`-r` — the
laptop uses a placeholder `luv-pending-<id>` and the remote renames it once the
clone lands.
If the target name is already taken by another live session, luv keeps the
session under `luv-myrepo-box-42-2` and warns rather than failing.

Each session also carries two tmux options, which is what `luv ls` reads:

```bash
tmux show-options -t luv-myrepo-box-42 -v @luv_id
tmux show-options -t luv-myrepo-box-42 -v @luv_workspace
```

## What runs where

| Command | Where it runs | tmux? |
|---|---|---|
| `luv config`, `luv --init` | Always local | – |
| `luv ls`, `luv continue` | Local, queries remote over SSH | – |
| `luv handover` | Local, drives both machines over SSH | starts one on the destination |
| `luv <repo> [prompt]` | Remote | yes |
| `luv <repo> <n>`, `-pr`, `-l` | Remote | yes |
| `luv <repo> -n` / `-r` | Remote | yes |
| `luv <repo> -nit` | Remote | **no** — streams `stream-json` back to your terminal |
| `luv --clean` | Remote (that's where the workspaces are) | no |

`--clean` refuses to delete a folder with a live tmux session unless you pass
`-f`.

## Docker repos

Repos with `.luv/settings.json` work unchanged. Because tmux wraps the whole
invocation, `docker compose up` runs *inside* the pane:

- Detaching leaves the containers running.
- Teardown happens when the agent exits, not when your SSH client goes away.
- The compose project name is still `luv-{repo}-{machine}-{number}`, so two workspaces on
  the same repo don't collide.

Install Docker on the remote and add your user to the `docker` group:

```bash
sudo apt install -y docker.io docker-compose-v2
sudo usermod -aG docker $USER    # log out and back in
```

## Troubleshooting

**`bash: luv: command not found`**
Your login shell can't find luv. Check with `ssh box 'bash -lc "command -v luv"'`.
Fix the remote's `PATH` (see step 3 above) or set
`luv config set remote.luv_bin /full/path/to/luv`.

**`bash: tmux: command not found`**
`sudo apt install tmux` on the remote.

**`Permission denied (publickey)`**
The key isn't installed or isn't being offered. Test with `ssh -v box`. Check
`~/.ssh/authorized_keys` on the remote and its permissions (`700` on `~/.ssh`,
`600` on the file). Point luv at the right key with
`luv config set remote.identity_file ~/.ssh/id_ed25519`, or `-i` for one run.

**`gh: To get started with GitHub CLI, please run: gh auth login`**
`gh` is authenticated on your laptop but not on the remote. luv numbers
workspaces with `gh api` *on the remote*, so it needs its own login there.

**My prompt didn't get sent**
Reopening a workspace that already has a live session attaches to it, and tmux
discards the command — so the prompt is dropped. luv prints a note when this can
happen. Type the prompt into the running agent instead.

**`luv ls` says a host is unreachable**
It shows that host's last known state rather than pruning; nothing is lost. A
configured host with no entries yet has no state to show, so it just says its
sessions aren't listed. When a host is gone for good, `luv ls --prune` forgets
its entries — and `luv config unset remote.hosts.<name>` stops the scan.

**Junk like `35;22;1M` appears at my prompt after a dropped connection**

That's mouse-tracking reports. The remote tmux/agent turned mouse tracking on
in *your* terminal and, having been killed along with the connection, never
turned it off — so every mouse move now types coordinates at your shell.
Bracketed paste (`200~` around pastes), a missing cursor, and a stuck
alternate screen come from the same cause.

luv now cleans this up itself: it stays alive as the parent of `ssh` and
restores the terminal whatever happens to the connection. If you land in this
state from something else, `reset` (or `stty sane` plus
`printf '\033[?1003l\033[?1006l\033[?2004l'`) clears it.

**Handover says the destination already has that folder**
Something with the same name is already there — either the same workspace from
an earlier handover, or one created on the machine that made this one. Look at
it first (`ssh box ls ~/prs`), then either `luv --clean` it or pass `-f` to
replace it.

**After a handover the agent started a fresh conversation**
luv prints a warning when it can't find a transcript for the workspace. The
usual cause is the workspace having been opened under a different path than the
one the agent recorded, or a Codex rollout stored somewhere other than
`~/.codex/sessions`. The workspace itself is unaffected — the code, branch, and
uncommitted work all moved.

**Handover left the session down**
The source folder is never deleted without `--purge`, so restart it where it
was: `luv <repo> <n> -r -s <source-host>`. luv prints that exact command before
it starts copying.

**A session is wedged**

```bash
ssh box tmux ls                          # see everything, not just luv's
ssh box tmux kill-session -t luv-myrepo-box-42
```

**I'm already inside tmux locally**
That's fine — `ssh` into a remote tmux from a local one nests, and `Ctrl-b`
goes to the outer session. Use `Ctrl-b Ctrl-b` for the inner one, or change the
prefix on one of them.
