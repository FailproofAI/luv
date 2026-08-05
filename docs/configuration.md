# Configuration

luv reads a single JSON file: `~/.luv/config.json`. It is created by
`luv config` and can be edited by hand.

- [The `luv config` command](#the-luv-config-command)
- [Keys](#keys)
- [Resolution order](#resolution-order)
- [Multi-host example](#multi-host-example)
- [Not this file](#not-this-file)

## The `luv config` command

```bash
luv config                       # interactive wizard
luv config list                  # show everything, as dotted keys
luv config get remote.host
luv config set remote.host box
luv config unset remote.host     # back to local-only
```

`set` parses the value as JSON when it can, so numbers, booleans, and lists work
as you'd expect:

```bash
luv config set remote.port 2222                    # → 2222 (number)
luv config set remote.ssh_opts '["-o","Compression=yes"]'
luv config set org exosphere                       # → "exosphere" (string)
```

Anything that isn't valid JSON is stored as a plain string, which is what you
want for hosts, paths, and org names.

`luv --init` still exists and sets only the org.

## Keys

### `org`

*string.* Default GitHub owner, used when you write `luv myrepo` instead of
`luv someorg/myrepo`. Set by `luv --init` or the wizard.

The **laptop** resolves this before dispatching, so a remote machine never needs
its own `org` configured.

### `machine`

*string, default: this machine's hostname.* A short name for **this** machine,
used in the workspace folder (`myrepo-mbp-43`) and branch (`luv-mbp-43`) of
everything created here.

It exists because every machine works out the next workspace number from
GitHub's issue counter independently, so a laptop and a box will happily pick
the same one — and without the slug they would push the same branch. The value
is lowercased, stripped to letters and digits, and capped at 8 characters, so
`Niveds-MacBook.local` becomes `nivedsma`; set it explicitly for something
readable:

```bash
luv config set machine mbp
```

The slug is stamped when a workspace is created and never changes afterwards —
`luv handover` moves `myrepo-mbp-43` to another machine under that same name,
because it records where the workspace came from and its branch may already be
pushed. Workspaces created before slugs existed keep working.

### `prs_dir`

*string, default `~/prs`.* Where workspace folders are created on **this**
machine. `~` is expanded.

### `shell_env`

*boolean, default `true`.* Whether a session luv starts should inherit your
shell's environment.

tmux and ssh exec their command directly, so a remote dispatch, a handover, or
a detached start never sources `~/.bashrc` or `~/.zshrc` — it runs with the
environment the tmux server was started with, frozen at whenever that server
first came up. Anything you export from your rc (the API key an agent
authenticates with, the `PATH` entry that makes `codex` resolvable) is simply
absent, and only for the sessions luv started for you.

With this on, luv runs `$SHELL -lic` on the machine the session runs on and
fills in what's missing before it clones or launches anything. Values already
set win, so `FOO=bar luv …` and `-e` still decide; `PATH` is merged rather than
replaced, with the rc's entries appended. An rc that never returns is abandoned
after 15 seconds.

Set it to `false` if your rc is slow, or if you want sessions to see only what
you pass them explicitly:

```bash
luv config set shell_env false
```

This is read on the machine the session runs on, so a remote host can opt out
independently of your laptop. It does not apply to Docker sessions — the
container has its own environment, and only `-e` crosses into it.

### `remote.host`

*string.* SSH destination — an `~/.ssh/config` alias (`box`), a `user@host`, or
a bare hostname. **Setting this turns on remote mode**: every workspace command
then runs there. Unset it, or pass `--local`, to run locally.

### `remote.identity_file`

*string.* Path to the SSH private key, passed as `ssh -i`. `~` is expanded by
luv, since `ssh` won't expand it when luv execs directly rather than through a
shell.

### `remote.port`

*number.* Passed as `ssh -p`. Omit for the default 22.

### `remote.dir`

*string.* Workspace root **on the remote**. Forwarded to the remote luv, which
uses it in place of its own `prs_dir`.

### `remote.luv_bin`

*string.* Full path to `luv` on the remote. Only needed when a login shell
there can't find it on `PATH` — see the
[troubleshooting section](remote-sessions.md#troubleshooting).

### `remote.ssh_opts`

*list of strings.* Extra arguments inserted into every `ssh` invocation, e.g.
`["-o", "ServerAliveInterval=30"]`. Prefer `~/.ssh/config` for anything durable.

### `remote.hosts.<name>`

*object.* Per-host overrides. Any of `identity_file`, `port`, `dir`, `luv_bin`,
or `ssh_opts` may be set here and takes precedence over the `remote.*` value of
the same name. This is what makes `-s otherbox` usable: without it, one global
key would be wrong for every host but the default.

### `ports`

*object.* Port forwarding. Servers an agent starts on a remote host are
detected and tunnelled to this machine — see
[remote-sessions.md](remote-sessions.md#reaching-servers-the-agent-started).
Every key is optional:

| Key | Default | Meaning |
| --- | --- | --- |
| `auto` | `true` | Forward the session you are attached to, as servers come and go. `false` leaves everything to `luv ports`. |
| `interval` | `10` | Seconds between re-checks while attached. Minimum 2. |
| `bind` | `"127.0.0.1"` | Local address the forwards listen on. `"0.0.0.0"` would publish the agent's dev server to your whole network. |
| `min` | `1024` | Ports below this are ignored; a dev server is not on 80. |
| `ignore` | `[]` | Specific ports never to forward. |
| `max_per_session` | `12` | Cap on forwards per session, so one stack cannot eat the local port space. |

```bash
luv config set ports.interval 5
luv config set ports.ignore '[5432, 6379]'
luv config set ports.auto false
```

Unlike `remote.*`, these are global — there is no `remote.hosts.<name>.ports`.

## Resolution order

**Host:** `-s HOST` → `remote.host`. If neither, luv runs locally.

**Per-host settings:** `-i PATH` (identity only) → `remote.hosts.<host>.<key>` →
`remote.<key>`.

**Workspace root:** `_LUV_PRS_DIR` (set by the dispatcher from `remote.dir`) →
`prs_dir` → `~/prs`.

**Org:** `org/repo` written explicitly → `org` from config → error.

`--local` overrides everything and is rejected alongside `-s` or `-i`, since
those combinations contradict each other.

## Multi-host example

```json
{
  "org": "exosphere",
  "machine": "mbp",
  "prs_dir": "~/prs",
  "remote": {
    "host": "box",
    "identity_file": "~/.ssh/id_ed25519",
    "dir": "~/prs",
    "ssh_opts": ["-o", "ServerAliveInterval=30"],
    "hosts": {
      "gpu-box": {
        "identity_file": "~/.ssh/gpu_key",
        "port": 2222,
        "dir": "/scratch/prs",
        "luv_bin": "/opt/venv/bin/luv"
      }
    }
  }
}
```

With this, `luv myrepo "…"` goes to `box` on port 22 with `id_ed25519`, and
`luv -s gpu-box ml "…"` goes to `gpu-box` on port 2222 with `gpu_key`, cloning
into `/scratch/prs` and using an explicit luv binary.

## Not this file

Two other files are sometimes confused with this one:

- **`~/.luv/sessions.json`** — the session registry, written by luv itself. Not
  configuration; see [sessions.md](sessions.md).
- **`<repo>/.luv/settings.json`** — a *repo-local* file, committed to the repo,
  whose only key is `compose_file`. It turns on Docker mode for that repo and
  has nothing to do with `~/.luv/config.json`.
