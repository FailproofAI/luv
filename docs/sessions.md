# The session registry

`luv ls` needs to answer "what am I running, and where?" — including for hosts
that are currently offline. It does that from `~/.luv/sessions.json`, a local
cache of every session luv has started, reconciled against live tmux state on
each run.

## Why it exists

The obvious approach — just ask each host `tmux ls` — has two gaps:

1. **luv wouldn't know which hosts to ask.** Once you use `-s` for a second
   machine, the config's default host is no longer the whole picture.
2. **tmux doesn't remember why a session exists.** The prompt you started with,
   the org, and the agent aren't tmux's business.

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
      "last_seen": 1753408899
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

`attached`, `activity`, and `live` are recomputed on every reconcile and are
deliberately **not** written back to the file.

## Reconciliation

On `luv ls`, `luv continue`, and `luv handover`, luv groups entries by host and
runs one query per host (concurrently when there's more than one):

```
tmux list-sessions -F '#{@luv_id}|#{session_name}|#{@luv_workspace}|#{session_attached}|#{session_activity}'
```

Each entry then lands in one of three states:

| State | Condition | Effect |
|---|---|---|
| **live** | Host answered and a session matched | Entry refreshed: name, workspace, attached, activity, `last_seen` |
| **dead** | Host answered and no session matched | Entry pruned |
| **unreachable** | Host never answered | Entry kept as-is, shown with `?`, warning printed |

Matching prefers `@luv_id` and falls back to the session name, which covers
sessions created before the option was stamped.

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

You lose the metadata luv can't recover — the prompt text, and which hosts to
look at — but no sessions. They're still running; find them with
`ssh <host> tmux ls` and reattach with `tmux attach -t <name>`.

A corrupt or unparseable file is treated as empty rather than crashing.
