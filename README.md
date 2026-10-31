# vankila

*Finnish for "jail".* Run Claude Code and pi with full permissions in a micro-VM, with your project
mounted, egress limited to an allow list, and API tokens that never enter the VM.

```console
$ vk auth claude     # once: paste a token from `claude setup-token`
$ cd ~/code/thing
$ vk claude          # that's it
```

The first run builds the image, which takes a few minutes. After that a sandbox boots in about a
second.

## Install

Needs Linux with KVM (`/dev/kvm`), and Python 3.11+.

```console
$ uv tool install .
$ vk doctor
$ vk selftest        # proves the isolation on real VMs, using dummy secrets
```

### Where smolvm comes from

[smolvm](https://smolmachines.com) runs unsandboxed on your host and handles your real tokens, so
`vk` looks for it in this order:

1. `$VANKILA_SMOLVM`
2. `smolvm` on your `PATH`, such as a distro package or your own build
3. A copy `vk` downloaded earlier

If it finds none, `vk` shows the URL and hash and asks, defaulting to no. Without a terminal it stops
instead of downloading. The archive must match the SHA-256 pinned in `src/vankila/smol.py`, so a
release changed after pinning is rejected. Pinning doesn't prove the pinned build itself is clean;
only building from source removes that trust. A copy from your `PATH` isn't checked against the pin,
and may be a version other than the one vankila was tested with (`vk --version` shows that version).

## Daily use

| Command | What it does |
|---|---|
| `vk claude [args]`, `vk pi [args]` | Run the harness in this project's sandbox |
| `vk shell` | A shell in the sandbox |
| `vk cargo test`, `vk <anything>` | Run any command in the sandbox |
| `vk status` | State, allow list and credentials |
| `vk blocked` | What the sandbox tried to reach and couldn't |
| `vk allow api.stripe.com` | Allow a host for this project |
| `vk auth [name]` | Show or store credentials |
| `vk trust` | Review and approve a changed `vankila.toml` |
| `vk ls`, `vk stop`, `vk rm [--state]` | Manage sandboxes |
| `vk build --refresh` | Rebuild the image with the latest tools |

Each project (nearest `vankila.toml`, else git root) gets its own VM, mounted at
`/workspace/<name>`. Your working directory carries over. Open as many sessions as you like: the VM
stops when the last one exits, and tells you if anything was blocked along the way.

## What's in the sandbox

- **Tools:** Node, npm, corepack, uv, Rust (with clippy and rustfmt), jq, gh, git, ripgrep, fd,
  build-essential, Claude Code, pi.
- **Egress:** deny by default, including DNS for names not on the list. Private ranges and cloud
  metadata are always blocked.
- **Registries:** a 14-day cooldown for npm, pnpm, yarn, bun, uv and pip, and install scripts off for
  the npm family. **Cargo has no cooldown** because it doesn't support one yet.
- **Harness config:** Claude Code starts with onboarding done, the folder trusted and permission
  prompts off (the VM is the boundary). Auto-updaters and non-essential traffic are off.
- **Git:** your host identity is passed through, and `git@github.com:` URLs use HTTPS.

## Configuration

No config is required. `vk init` writes a starter file with presets detected from your project.
Settings merge in this order: built-in defaults, `~/.config/vankila/config.toml`, then the project's
`vankila.toml`. A later value replaces an earlier one, including lists, with one exception: hosts in
the global `network.allow` always apply, in addition to the project's.

```toml
[network]
presets = ["claude", "github", "npm"]   # anthropic claude github npm pypi crates rust apt
                                        # openai openrouter mistral google-ai
allow = ["api.stripe.com", "*.example.com"]
allow_private = ["10.20.0.0/16"]        # e.g. an internal registry

[sandbox]
cpus = 4
memory = 8192                           # MiB; a cap, only touched memory is used
overlay = 20                            # GiB, for the VM's filesystem changes
storage = 20                            # GiB
ports = ["3000", "8080:80"]
mounts = ["../shared-lib:/workspace/shared-lib:ro"]
env = { RUST_BACKTRACE = "1" }
setup = ["uv sync"]                     # once per new sandbox, as the agent user, under the allow list
ssh_agent = false                       # forward the host agent (keys stay out, signing is allowed)
keep_running = false                    # leave the VM up after the last session

[tools]                                 # a change builds a new image, shared by matching projects
base = "debian:trixie-slim"
node = "24"
rust = "stable"
apt = ["postgresql-client"]

[registries]
cooldown_days = 14                      # 0 turns it off
ignore_scripts = true

[auth]
claude = "oauth"                        # or "api-key", when both are configured

[credentials.stripe]                    # a secret the sandbox can use but never see
env = "STRIPE_API_KEY"                  # the sandbox gets a placeholder in this variable
hosts = ["api.stripe.com"]              # the real value is only ever sent here
command = "pass show stripe"            # optional; or $STRIPE_API_KEY, or `vk auth stripe`
# set_header = "authorization"          # optional; the host sets the whole header instead

[harness.claude]
skip_permissions = true
settings = { model = "opus" }           # merged into ~/.claude/settings.json in the guest

[harness.pi]
settings = { defaultProvider = "anthropic" }  # merged into ~/.pi/agent/settings.json
```

### Harness files

Files laid over the harness config directory on every start, global first and then project:

```
~/.config/vankila/harness/claude/        CLAUDE.md, skills/, agents/, ...
~/.config/vankila/harness/pi/            AGENTS.md, models.json, ...
<project>/.vankila/harness/{claude,pi}/  the same, for one project
```

Harness sessions live on the host in `~/.local/state/vankila/projects/`, so they outlast the VM.
That makes recreating a sandbox cheap. Anything installed by hand in the VM is lost when it is
recreated, so put it in `setup` or `tools.apt`.

## Credentials

A value is looked up from the host's environment variable, then `vk auth`
(`~/.config/vankila/secrets.toml`, mode 0600), then a command. Real values reach smolvm only through
the environment of `machine start`, and are never written to the VM, its record or a log.

| Name | Guest variable | Sent to | Used by |
|---|---|---|---|
| `claude` | `CLAUDE_CODE_OAUTH_TOKEN` | `api.anthropic.com` | Claude Code |
| `anthropic` | `ANTHROPIC_API_KEY` | `api.anthropic.com` | Claude Code, pi |
| `github` | `GH_TOKEN` | `api.github.com` (and `github.com` for git) | gh, git; found with `gh auth token` |
| `openai`, `openrouter`, `mistral`, `gemini` | `OPENAI_API_KEY`, etc. | their API host | pi; also add the matching network preset (`gemini` uses `google-ai`) |

Inside the VM, `echo $ANTHROPIC_API_KEY` prints a placeholder. smolvm swaps it for the real value at
the VM boundary, in request headers only, on HTTPS to the listed hosts. Any other host gets at most
the meaningless placeholder, never the real value.

**Pi and a Claude subscription:** pi only treats a token as a subscription token when it sees the
`sk-ant-oat` prefix, which a placeholder doesn't have. So boundary injection for pi needs an API key.
Pi's own `/login` still works, but then its token is stored inside the sandbox state.

## Security model

**Enforced on the host, so a compromised guest can't change it:** the egress allow list, the
credential boundary, and the VM itself. Everything in the guest, including the cooldowns and harness
settings, is sensible defaults: the agent has passwordless sudo and could change them.

Protections against a guest attacking the host:

- **`vankila.toml` needs approval.** The agent can edit it and it can arrive in a cloned repo, and it
  controls mounts, egress and secret commands. New or changed content shows a diff and must be
  approved on the host. A non-interactive run refuses.
- **Guest-writable directories are untrusted.** Host-side writes refuse to follow symlinks, and
  overlays don't copy them.
- **Home is never mounted.** `vk` refuses to run with `~` or `/` as the project, or to mount them.

Limits you should know about:

- **The project mount is live.** The agent can change anything in it, and that may run on your host
  later: git hooks, `.envrc`, build scripts, editor config. `vk` warns when `.git/hooks` or
  `.git/config` change during a session, but it can't cover the rest.
- **`.env` files are visible.** Anything in the project directory is readable in the guest.
- **A credential can still be used.** The agent can't read your GitHub token, but it can act with it
  on GitHub, including pushing somewhere. Use a fine-grained token.
- **Allowed hosts can receive data.** An agent can upload to anything on the list, and `github.com`
  and the package registries are on it by default. An allow list limits exfiltration; it doesn't stop it.

### Boundary limits

These come from smolvm's interception on credential hosts:

- **No WebSockets or `CONNECT`** on those hosts, including `github.com` when a GitHub token is bound.
- **Request bodies over 8 MiB are refused** there. A large `git push` over HTTPS can hit this; set
  `sandbox.ssh_agent = true` and use an SSH remote if so.
- **HTTP/1.1 only** from the guest side. Responses still stream.
- A rotated secret takes effect at the next VM start (`vk stop`).

## Development

```console
$ python3 -m unittest discover -s tests   # host-side logic, against a fake smolvm
$ uv run vk selftest                      # real VMs; needs KVM
```

| Path | Purpose |
|---|---|
| `src/vankila/cli.py` | Commands |
| `src/vankila/config.py` | Config loading, presets |
| `src/vankila/trust.py` | Approval of `vankila.toml` |
| `src/vankila/secrets.py` | Credential lookup and binding |
| `src/vankila/sandbox.py` | Lifecycle, sessions, drift handling |
| `src/vankila/seed.py` | Registry and harness config |
| `src/vankila/image.py`, `assets/provision.sh` | The golden image |
| `src/vankila/selftest.py` | End-to-end checks |
