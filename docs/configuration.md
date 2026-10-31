# Configuration reference

No configuration is required. This page covers where vankila keeps its files, how to give every sandbox
your harness config, and every `vankila.toml` option.

## Where files live

Paths follow the XDG variables, shown here with their defaults.

### Config (`$XDG_CONFIG_HOME/vankila`, default `~/.config/vankila`)

| Path | Purpose |
|---|---|
| `config.toml` | Global settings for all projects. Same format as `vankila.toml` |
| `secrets.toml` | Secrets stored by `vk auth`, mode 0600. Edit it through `vk auth` |
| `harness/claude/` | Files laid over `~/.claude` in every sandbox |
| `harness/pi/` | Files laid over `~/.pi/agent` in every sandbox |

### Project

| Path | Purpose |
|---|---|
| `<project>/vankila.toml` | Project settings. Must be approved before it applies |
| `<project>/.vankila/harness/{claude,pi}/` | Harness files for this project only |

The project root is the nearest directory upwards with a `vankila.toml`, else the nearest with `.git`,
else the current directory. It is mounted at `/workspace/<name>`.

### State (`$XDG_STATE_HOME/vankila`, default `~/.local/state/vankila`)

| Path | Purpose |
|---|---|
| `projects/<machine>/claude/` | The guest's `~/.claude`: sessions, memory, copied harness files |
| `projects/<machine>/pi/` | The guest's `~/.pi` |
| `projects/<machine>/snapshots/` | Project copies, when `sandbox.snapshot` is on |
| `trust/<machine>.toml` | The approved copy of the project's `vankila.toml` |

`<machine>` is `vk-<name>-<hash>`, where the hash comes from the project's absolute path. Moving or
renaming a project therefore gives it a new sandbox and new state, and its `vankila.toml` needs approving
again. `vk status` prints the machine name.

`vk rm` keeps this state. `vk rm --state` deletes `projects/<machine>/`, but not the approval.

### Data (`$XDG_DATA_HOME/vankila`, default `~/.local/share/vankila`)

| Path | Purpose |
|---|---|
| `images/<hash>/` | Built images, shared by projects with the same `[tools]` |
| `images/<hash>.log` | The log of the image build |
| `smolvm/` | The smolvm copy that `vk` downloads, if you accept its offer |

## Harness config

To give every sandbox your `CLAUDE.md`, `AGENTS.md`, skills and so on, put them in the global overlay.
You don't need to copy anything into `projects/<machine>/`.

```
~/.config/vankila/harness/claude/        -> ~/.claude/ in the guest
    CLAUDE.md
    skills/<name>/SKILL.md
    agents/...

~/.config/vankila/harness/pi/            -> ~/.pi/agent/ in the guest
    AGENTS.md
    models.json
    skills/...

<project>/.vankila/harness/{claude,pi}/  -> the same, for one project
```

Files are copied each time `vk` runs something in the sandbox, global first and then project. New and
existing projects both pick them up.

- **Files merge one by one.** A project's `CLAUDE.md` replaces the global one entirely. Differently named
  skills from both levels sit side by side.
- **The overlay wins over the guest.** An edit made inside the sandbox to an overlaid file is overwritten
  on the next run, so make changes in the overlay.
- **Deleting doesn't propagate.** A file removed from an overlay stays in projects that already received
  it, until you delete it from `projects/<machine>/` as well.
- **Only regular files are copied.** Symlinks are skipped, including a symlinked directory, so you can't
  link these files to your dotfiles.
- **Project overlays aren't approved.** `vk trust` covers `vankila.toml` only. A project overlay can come
  from a cloned repo or be written by the agent, and it is applied without review. It is copied by the
  guest and only into the guest's harness config, but it can set that harness's instructions and hooks.

Your repo's own `CLAUDE.md` or `AGENTS.md` needs none of this, because the harness reads it from the
mounted project.

### Harness settings

`settings.json` is the one file that merges rather than replaces. After the overlays are copied, the
values from `[harness.claude] settings` and `[harness.pi] settings` are merged into it, and they win over
what the file already holds.

vankila also sets these in Claude Code's `settings.json`, and you can override them:

```toml
[harness.claude.settings]
skipDangerousModePermissionPrompt = true

[harness.claude.settings.env]
DISABLE_AUTOUPDATER = "1"
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = "1"
```

## How settings combine

Settings merge in this order: built-in defaults, `config.toml`, then `vankila.toml`.

- **Scalars and lists:** a later value replaces an earlier one. A project's `presets` replaces the
  global list instead of adding to it.
- **Tables:** merge key by key, as with `sandbox.env`, `credentials` and harness `settings`.
- **The exception:** hosts in the global `network.allow` always apply, in addition to the project's.

An unknown section is an error, and so is an unknown key, except under `[credentials]` and `[harness]`,
where it is ignored.

### Approval

The agent can write to `vankila.toml`, and the file can arrive in a cloned repo, so it applies only once
you approve that exact content. A new or changed file prompts you with its text or a diff. Without a
terminal, `vk` stops and asks you to run `vk trust`. Deleting an approved file also needs approval,
because the defaults may allow more. `-y` doesn't skip any of this.

`vk init` and `vk allow` approve what they write themselves. `config.toml` needs no approval, because the
guest can't reach it.

### When a change takes effect

| Setting | Takes effect |
|---|---|
| `network.presets`, `network.allow` | On the next `vk` run or `vk allow`, without a restart |
| `harness.*`, `auth.claude` | On the next `vk` run |
| `registries.*`, `sandbox.snapshot` | The next time the VM starts (`vk stop` forces this) |
| `sandbox.keep_running` | When the last session exits |
| `tools.*`, `network.allow_private`, every other `sandbox.*` key | The sandbox is recreated |
| Which credentials are bound, and their hosts | The sandbox is recreated |

Recreating asks first and shows what changed. `-y` accepts. Without a terminal or `-y`, the existing
sandbox keeps running with its old settings. Your project and harness sessions survive, but anything
installed by hand in the VM doesn't, and `sandbox.setup` runs again.

## `vankila.toml` options

Every section is optional, and `config.toml` accepts the same options. `vk init` writes a starter file,
with network presets detected from your project.

### `[network]`

| Key | Default | Meaning |
|---|---|---|
| `presets` | `["claude", "github", "npm", "pypi", "crates", "rust", "apt"]` | Host groups to allow |
| `allow` | `[]` | More hosts: `api.example.com`, or `*.example.com` for subdomains only |
| `allow_private` | `[]` | Private CIDR ranges to allow, such as `"10.20.0.0/16"` |

Everything else is blocked. `vk blocked` lists what the sandbox tried to reach.

| Preset | Hosts |
|---|---|
| `anthropic` | `api.anthropic.com` |
| `claude` | `api.anthropic.com`, `claude.ai`, `platform.claude.com` |
| `github` | `github.com`, `api.github.com`, `codeload.github.com`, `*.githubusercontent.com` |
| `npm` | `registry.npmjs.org`, `registry.yarnpkg.com` |
| `pypi` | `pypi.org`, `files.pythonhosted.org` |
| `crates` | `crates.io`, `index.crates.io`, `static.crates.io` |
| `rust` | `static.rust-lang.org` |
| `apt` | `deb.debian.org` |
| `openai` | `api.openai.com` |
| `openrouter` | `openrouter.ai` |
| `mistral` | `api.mistral.ai` |
| `google-ai` | `generativelanguage.googleapis.com` |

The allow list limits accidents, not a determined guest. See
[Known weaknesses](../SANDBOX.md#known-weaknesses).

### `[sandbox]`

| Key | Default | Meaning |
|---|---|---|
| `cpus` | `4` | vCPUs |
| `memory` | `8192` | MiB. A cap: only touched memory is used |
| `overlay` | `20` | GiB, for persistent changes to the VM's root filesystem (installed packages, caches, files outside the mounts). Raise it on "no space left on device" |
| `storage` | `20` | GiB, for image layers and container data. Matters for large images or containers run inside the VM |
| `ports` | `[]` | Forwarded ports: `"3000"` or `"8080:80"` |
| `mounts` | `[]` | Extra mounts: `"HOST:GUEST"` or `"HOST:GUEST:ro"` |
| `env` | `{}` | Guest environment variables. Visible to the guest, so not for secrets |
| `setup` | `[]` | Commands run once per new sandbox, as the agent user, under the allow list |
| `ssh_agent` | `false` | Forward the host's agent. Keys stay out, but signing is allowed. Needs `SSH_AUTH_SOCK` |
| `keep_running` | `false` | Leave the VM up after the last session exits |
| `snapshot` | `false` | Copy the project before each VM start, keeping the last 3 |

A relative mount source is resolved from the project root, and `~` expands. `vk` refuses a source that
overlaps credentials or code your host runs, such as `~/.ssh`, `~/.config` or `/etc`, and one that goes
through a symlink inside the project.

If a `setup` command fails, `vk` stops. Fix it, then run `vk rm` and try again.

### `[tools]`

These define the image. Projects with the same values share one, and a change builds a new one.
`vk build --refresh` rebuilds it with the latest tools.

| Key | Default | Meaning |
|---|---|---|
| `base` | `"debian:trixie-slim"` | Base image |
| `node` | `"24"` | Node version |
| `rust` | `"stable"` | Rust toolchain |
| `apt` | `[]` | Extra Debian packages |

### `[registries]`

| Key | Default | Meaning |
|---|---|---|
| `cooldown_days` | `14` | Refuse packages newer than this, for npm, pnpm, yarn, bun, uv and pip. `0` turns it off. Cargo doesn't support a cooldown |
| `ignore_scripts` | `true` | Turn off install scripts for npm, pnpm, yarn and bun |

Changing `cooldown_days` to `0` doesn't remove `/etc/uv/uv.toml` or `/etc/pip.conf` from an existing
sandbox, so uv and pip keep the old cooldown until you `vk rm`.

### `[auth]`

| Key | Default | Meaning |
|---|---|---|
| `claude` | `"oauth"` | Which credential `vk claude` uses when both are set. `"api-key"` keeps the API key, which outranks the subscription token |
| `github` | `"stored"` | `"stored"` binds only a token from `vk auth github`. `"gh"` also uses `$GH_TOKEN` and `gh auth token` |

### `[credentials.<name>]`

A secret the sandbox can use but never see. The built-in credentials need no entry here: see
[Credentials](../README.md#credentials).

```toml
[credentials.stripe]
env = "STRIPE_API_KEY"
hosts = ["api.stripe.com"]
command = "pass show stripe"
```

| Key | Required | Meaning |
|---|---|---|
| `env` | Yes | The guest variable, which holds only a placeholder |
| `hosts` | Yes | The only hosts the real value is sent to. They are added to the allow list |
| `command` | No | A host command that prints the value. It runs through your shell, with a 30-second timeout |
| `set_header` | No | A header the host sets to the whole value, for when a placeholder can't be substituted |

The value comes from the host's `$<env>`, then `vk auth <name>`, then `command`. With no value the
credential isn't bound, and `vk` warns you. Bound hosts have some limits: see
[Credential boundary limits](../SANDBOX.md#credential-boundary-limits).

### `[harness.claude]`

| Key | Default | Meaning |
|---|---|---|
| `skip_permissions` | `true` | Pass `--dangerously-skip-permissions` to `vk claude` |
| `settings` | `{}` | Merged into `~/.claude/settings.json` in the guest |

### `[harness.pi]`

| Key | Default | Meaning |
|---|---|---|
| `settings` | `{}` | Merged into `~/.pi/agent/settings.json` in the guest |

## Environment variables

| Variable | Effect |
|---|---|
| `XDG_CONFIG_HOME`, `XDG_STATE_HOME`, `XDG_DATA_HOME` | Move the directories above |
| `VANKILA_SMOLVM` | The smolvm binary to use. See [Where smolvm comes from](../SANDBOX.md#where-smolvm-comes-from) |
