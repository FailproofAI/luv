# The session registry

`luv ls` needs to answer "what is running, and where?" — including for hosts
that are currently offline. It does that from `~/.luv/sessions.json`, a local
cache of every session luv has started, reconciled against live tmux state on
each run.

## Why it exists

The registry is not the source of truth for *what is running* — the hosts are.
It exists for the two things asking `tmux ls` cannot tell you:

1. **Which hosts to ask.** Once you use `-s` for a second machine, the config's
   default host is no longer the whole picture.
2. **Why a session exists.** The prompt you started with, the org, and the agent
   aren't tmux's business.

There's also a sequencing problem. Your laptop can't know the workspace number
in advance — it comes from `gh api` running *on the remote* — so at dispatch
time there is nothing stable to record. luv solves that by generating a random
token, writing it into the registry, and having the remote stamp it onto the
tmux session as a `@luv_id` option. Matching happens on that token, so renaming
the session (which luv does as soon as the clone lands) breaks nothing.

## Schema

```json
{
  "sessions": [
    {
      "id": "k3f9a2c1",
      "host": "box",
      "session": "luv-myrepo-box-42",
      "org": "exosphere",
      "repo": "myrepo",
      "workspace": "myrepo-box-42",
      "agent": "claude",
      "model": null,
      "prompt": "fix the flaky test",
      "created": 1753401234,
      "last_seen": 1753408899,
      "pr_number": 42,
      "pr_url": "https://github.com/exosphere/myrepo/pull/42",
      "pr_state": "OPEN",
      "pr_checked": 1753408899
    }
  ]
}
```

| Field | Meaning |
|---|---|
| `id` | Random 8-char token; matches the session's `@luv_id` tmux option |
| `host` | The `remote.host` value used, or absent for a local session |
| `session` | tmux session name; starts as `luv-pending-<id>` and is corrected on the first reconcile |
| `org`, `repo` | GitHub owner and repo, resolved on the laptop |
| `workspace` | Folder name (`myrepo-box-42`); `null` until the remote reports it. The middle part names the machine that created it — see [configuration.md](configuration.md#machine) |
| `agent` | `claude` or `codex` |
| `model` | The `-m` value, or `null` for the agent's default; replayed by `luv handover` |
| `prompt` | The prompt you launched with, for the `luv ls` label |
| `created` | Unix time the entry was written |
| `last_seen` | Unix time of the last successful reconcile |
| `pr_number`, `pr_url` | The session's pull request; `null` until one exists |
| `pr_state` | `OPEN`, `CLOSED` or `MERGED` — what `luv rm --merged` selects on |
| `pr_checked` | Unix time GitHub was last asked about it |
| `pr_hint` | PR number known at dispatch (`-l` / `-pr` only); absent otherwise |
| `adopted` | Present when this machine found the session rather than starting it |

`attached`, `activity`, and `live` are recomputed on every reconcile and are
deliberately **not** written back to the file.

## Reconciliation

`luv ls`, `luv continue`, `luv rm` and `luv handover` scan every host luv knows
about — every one with a registry entry, every one in the config (`remote.host`
and each `remote.hosts.<name>`), and always the local machine. One query per
host, run concurrently:

```
tmux list-sessions -F '#{@luv_id}|#{session_name}|#{@luv_workspace}|#{session_attached}|#{session_activity}'
```

A session counts as luv's if it is named `luv-*` or carries a `@luv_workspace`
option — the second covers a local run inside a tmux you opened and named
yourself, which luv never renames.

Each entry then lands in one of three states:

| State | Condition | Effect |
|---|---|---|
| **live** | Host answered and a session matched | Entry refreshed: name, workspace, attached, activity, `last_seen` |
| **dead** | Host answered and no session matched | Entry pruned |
| **unreachable** | Host never answered | Entry kept as-is, shown with `?`, warning printed |

Matching prefers `@luv_id` and falls back to the session name, which covers
sessions created before the option was stamped.

## Sessions started somewhere else

A registry only ever records what *that* machine dispatched. Left there, a
session started from your laptop would be invisible from your desktop even
though both are looking at the same tmux server — and the laptop is exactly the
machine you don't have with you.

So a live session that no entry claims is **adopted**: written into the registry
as a new entry, flagged `adopted`, and listed like any other. From that point it
is a normal entry — `luv continue` attaches to it, `luv rm` tears it down, and
the next reconcile matches it on its `@luv_id` rather than adopting it twice.

An adopted entry carries only what tmux knows: host, session, workspace,
attached, activity. The prompt and the agent belonged to the machine that
started the session, so those columns show `-` rather than a guess. The org and
repo are the exception, because the PR column is useless without them — one
round trip per host reads `git remote get-url origin` in each adopted workspace,
and the answer is cached in the registry like everything else:

```
for w in myrepo-box-42 myrepo-box-51; do echo "$w|$(git -C ~/prs/"$w" remote get-url origin)"; done
```

Reading them off the folder name instead is not an option: `myrepo-box-42`
cannot be split into repo and slug without already knowing one of the two, and
the configured default org would be the wrong answer for every repo that isn't
in it — a wrong PR link is worse than none.

**An unreachable host never causes pruning.** This is the rule the whole design
protects: running `luv ls` on a plane, or while a box is rebooting, must not
silently erase your session list. luv distinguishes the two cases by ssh's own
exit code — 255 means ssh failed to connect, anything else means the host
answered (an empty result from a host with no tmux server is a legitimate
"nothing is running here").

To forget a host you've genuinely retired:

```bash
luv ls --prune
```

That drops entries for hosts that didn't answer, in addition to the dead ones
reconciliation already removed.

## Handover

`luv handover` replaces the moved session's entry rather than editing it: the
old one is dropped and the destination writes a fresh entry, with a new `id` and
`@luv_id`, carrying the original `org`, `repo`, `agent`, `model`, and `prompt`.
The `workspace` name is unchanged, slug and all.

Note that the registry only ever learns about sessions luv **dispatched to a
host** — a `luv --local` run records nothing. So a workspace on the machine
you're sitting at is normally absent from it, which is why handover falls back
to looking on disk instead of treating a miss as an error.

## PR links

Every luv workspace is one pull request by construction — folder
`{repo}-{machine}-{N}`, branch `luv-{machine}-{N}` — so `luv ls` can show the PR
each session is producing:

```
HOST  SESSION             WORKSPACE       AGENT   ATTACHED  ACTIVE  PR    PROMPT
box   luv-myrepo-box-42   myrepo-box-42   claude  yes       2m ago  #42   fix the flaky test
box   luv-myrepo-box-51   myrepo-box-51   codex   no        1h ago  -     add rate limiting
```

`#42` is an OSC 8 terminal hyperlink, so ctrl/cmd-click opens the PR. When
stdout isn't a terminal there's nothing to click and a bare number would be
useless, so `luv ls | grep` and redirects get the full URL instead.

The lookup asks GitHub for the PR whose head branch is the workspace's own:

```
gh pr list --repo {org}/{repo} --head luv-{machine}-{N} --state all --limit 1 --json number,url
```

The branch is read back off the folder name rather than rebuilt from this
machine's slug — the folder keeps the slug of whichever machine created it, both
after a handover and for a session another machine started. A pre-slug folder
implies a pre-slug `luv-{N}` branch, which is what it gets asked for.

`gh pr list` rather than the REST endpoint because its `--head` takes a bare
branch name. REST wants `head={owner}:{branch}`, and the owner luv recorded is
whatever you typed — which stops matching the moment the org is renamed or the
repo is transferred.

It is a head query and nothing else. Checking whether PR `#N` merely *exists*
would show a stranger's PR whenever someone took that number between luv
reserving the folder and the agent pushing — the folder number is only ever the
*intended* PR number. Sessions started from an existing PR (`luv -l <url>`,
`luv <repo> -pr <N>`) are the exception: there the number is known at dispatch
and stored as `pr_hint`, so they resolve with no network call at all. Their
branch is the PR's own head ref, which the head query would never match.

Results are cached in the registry so repeat runs are instant and a link stays
on screen when GitHub is unreachable — 5 minutes for a PR that was found, 1
minute when there wasn't one yet (the agent may open it any moment). A session
with no PR, no org, or a folder that isn't a workspace of its repo shows `-`.

`luv ls --no-pr` skips the whole thing for a fast, offline-safe listing, which
is also what happens automatically when `gh` isn't installed. `luv continue`
renders the cached links but never triggers a lookup of its own.

## Removing sessions

`luv rm` is the teardown counterpart to `luv ls`: it kills the tmux session and
deletes the workspace folder on whichever host the session lives on, then drops
the registry entry.

```bash
luv rm myrepo-box-42      # by workspace, or by session name
luv rm --merged           # every session whose PR state is MERGED
luv rm --dead             # workspaces on a host with no live luv session
luv rm --dead --host box  # scope either selector to one machine
```

`--merged` reads the `pr_state` the PR lookup already caches, so "clean up
what's landed" costs nothing beyond what `luv ls` was doing anyway.

`--dead` exists because **reconciliation removes a registry entry the moment its
tmux session dies, but nothing removes the clone**. Those folders are invisible
to `luv ls` — the registry has already forgotten them — and they are usually
what is actually consuming the remote's disk. Finding them means asking the host
directly: `ls -1` the workspace root, `tmux list-sessions`, and take the
difference.

That same asymmetry is why a named target falls back to a folder scan. A session
you have just finished with is exactly one whose entry reconciliation has
already dropped, so looking only in the registry would fail for the most common
case.

Two things bound the damage:

- Only names ending in `-{N}` are eligible — `{repo}-{machine}-{N}` and pre-slug
  `{repo}-{N}` alike — checked again immediately before the `rm -rf` rather than
  only at selection time. A stray directory in the workspace root is never a
  candidate, and a target that resolves to one is an error, not a deletion.
- A named target is its own confirmation, but `--merged` and `--dead` print what
  they matched and ask, because a selector can sweep up folders on a machine you
  are not looking at. `-f` skips the prompt.

A failed delete — unreachable host, permissions — leaves the registry entry in
place, so the session doesn't silently disappear from `luv ls` while its files
are still on disk.

## Concurrency

Both dispatch (append an entry) and `ls` (prune entries) are read-modify-write,
which an atomic file replace alone doesn't protect — two luv invocations at once
would lose one entry. luv takes a lock at `~/.luv/sessions.lock` around the
whole cycle, using `O_CREAT | O_EXCL`.

A lock older than the timeout is assumed to belong to a killed process and is
broken. If the lock still can't be taken, luv proceeds **without** it: losing a
registry entry is a far better outcome than refusing to launch an agent.

## Recovery

The registry is a cache. Deleting it is safe:

```bash
rm ~/.luv/sessions.json
```

You lose the metadata luv can't recover — the prompt text, and any host that is
neither configured nor the local machine — but no sessions. Everything running
on a host luv still knows about is adopted back on the next `luv ls`. For a host
that fell out of the set entirely, `ssh <host> tmux ls` and
`tmux attach -t <name>` still work, or add it back with `-s <host>`.

A corrupt or unparseable file is treated as empty rather than crashing.
