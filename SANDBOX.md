# Security

vankila runs coding agents with full permissions, on the assumption that the agent, or a repo it pulled
in, may be hostile. The aim is that a compromised guest can't run code on your host, read your host's
secrets, or walk off with your tokens. It doesn't fully stop data leaving the sandbox.

## At a glance

**Sandbox guards**

| Guard | What it stops |
|---|---|
| KVM micro-VM | Guest code, even as root, reaching the host outside its mounts |
| Credential boundary | The guest reading real tokens; it only ever holds placeholders |
| Egress allow list | Accidental traffic to unlisted hosts; [can be bypassed](#known-weaknesses) |
| Private range block | Reaching the host's loopback, the LAN and cloud metadata |
| DNS filter | Resolving names that aren't on the allow list, through any resolver |
| `vankila.toml` approval | The agent or a cloned repo changing mounts, egress or secret commands unseen |
| No host writes to guest state | A planted symlink redirecting a `vk` write onto your host |
| Mount denylist | Mounting `~`, `/`, credentials or system directories |
| VM process confinement | A VMM bug reading the rest of your home, on kernels with Landlock |
| Opt-in GitHub token | A sandbox silently getting whatever your host's `gh` is logged in with |

**Compromises for usability**

| Compromise | Why | What it costs | How to reduce it |
|---|---|---|---|
| Passwordless sudo in the guest | Agents need to install things | Guest-side settings, such as cooldowns, are only defaults | Nothing to set: rely on the guards above |
| Permission prompts off | Autonomy is the point | The agent acts without asking | `harness.claude.skip_permissions = false` |
| Live read-write project mount | Copying a project on every run costs too much | The agent can change or delete anything in it, including `.git` | `sandbox.snapshot = true` |
| Project files can later run on your host | Follows from the live mount | Git hooks, `.envrc` and build scripts can run outside the VM | Review the warning; hook and config changes are warned about, not blocked |
| Broad default egress | Zero-config startup | `github.com` and the registries can receive data | `vk init`, then trim the presets |
| IP-based egress filter | The hostname-aware options can't be combined with the credential boundary | The allow list won't stop deliberate exfiltration | None yet: don't give the sandbox data that must not leave |
| TLS interception CA in the guest | Required to inject credentials | The guest trusts the boundary's CA, though it can only attack itself with it | Nothing to set |
| Bound credentials stay usable | The agent has to be able to do its work | It can act within a token's scopes, and that outlasts the VM | Use narrowly scoped tokens |
| Sessions persisted on the host | Makes recreating a VM cheap | Harness state on the host is guest-writable | `vk rm --state` drops it |
| Raw terminal output in interactive sessions | Filtering would need a terminal proxy | Escape sequences reach your terminal | Turn off OSC 52 in your terminal |

Only what's enforced on the host counts as a guard. Everything in the guest, including the registry
cooldowns and harness settings, is a sensible default the agent could change.

`vk selftest` checks the VM, credential, network, planted-symlink and confinement guards on real VMs,
using dummy secrets. Run it after upgrading smolvm.

## How the guards work

**Credentials.** Inside the VM, `echo $ANTHROPIC_API_KEY` prints a placeholder. smolvm swaps it for the
real value at the VM boundary, in request headers only, on HTTPS to the listed hosts. Any other host gets
at most the meaningless placeholder, never the real value. Real values reach smolvm only through the
environment of `machine start`, and are never written to the VM, its record or a log.

- **`vankila.toml` needs approval.** The agent can edit it and it can arrive in a cloned repo, and it
  controls mounts, egress and secret commands. New or changed content shows a diff and must be
  approved on the host, and deleting an approved file counts as a change. A non-interactive run refuses.
  The file is read once, so what gets approved is what gets used.
- **`vk` doesn't work in guest-writable state.** The guest writes its own harness config, so a planted
  symlink resolves inside the guest. The host side only creates the mount roots.
- **Sensitive paths are never mounted.** `vk` refuses `~`, `/`, and anything overlapping credentials or
  system directories, such as `~/.ssh`, `~/.config`, `~/.local` and `/etc`. There's no override. A mount
  source inside the project can't be a symlink.
- **The VM process is confined.** On kernels with Landlock it reaches only smolvm's data, the system
  paths it needs, and the declared mounts, not the rest of your home. If Landlock is missing, `vk` warns
  and starts anyway. `VANKILA_NO_CONFINE=1` turns this off, and only on the host.

## Known weaknesses

The first group lets a guest get data out or act beyond the project. `vk selftest` re-checks the egress
ones and prints them as `known weakness`, so a smolvm upgrade that changes them shows up.

- **The egress allow list can be bypassed.** smolvm enforces it by destination IP, not by hostname or
  port. Treat it as a limit on accidents and spend, not as protection against exfiltration:
  - A guest can reach any site that shares a CDN address with an allowed host.
  - Any port on an allowed host's address connects.
  - A `*.domain` entry lets the guest look up any name under that domain, which passes data to that
    domain's name servers. Exact entries don't have this problem: smolvm answers port 53 traffic to any
    address itself, and refuses names that aren't on the list.
  - Allowed hosts can receive data themselves, and `github.com` and the registries are on the default
    list.

  smolvm's own interception modes could check hostnames, but its docs say they can't be combined with
  `--credential`, so using them would mean replacing the credential boundary.
- **A credential can still be used.** The agent can't read a bound token, but it can act with it within
  its scopes, and that outlasts the VM. Use a fine-grained GitHub token.
- **The project mount is live.** The agent can change or delete anything in it, including `.git`. Turn
  on `sandbox.snapshot` for a copy to go back to. Snapshots aren't taken while the VM is already running,
  and `vk rm --state` deletes them.
- **`.env` files are visible.** Anything in the project directory is readable in the guest.
- **In-repo harness config isn't approved.** `vk trust` covers only `vankila.toml`. Files in
  `.vankila/harness/` can come from a cloned repo or the agent, and are copied into the guest's harness
  config on every run with no prompt. That includes instructions and hooks, which run in the guest and
  can use its bound credentials and allowed hosts. Check that directory before running `vk` in a repo you
  don't trust.

These let guest changes affect your host later, or use up its resources:

- **Project files can run on your host.** Git hooks, `.envrc`, build scripts and editor config all can.
  `vk` warns when git hooks or config change, including in worktrees and submodules, but it blocks
  nothing, and it warns only as a session ends, or as the next one starts with `keep_running`. A shell
  prompt that runs git can trigger a hook before you read the warning. Other files aren't watched.
- **Disk and API spend have no limit.** VM disks are capped, but the mounts can fill the host's disk, and
  `vk` only warns when space is low.
- **Interactive guest output reaches your terminal unfiltered.** Under `vk shell` or `vk claude`, a guest
  can send escape sequences, including clipboard writes on terminals that honour OSC 52. Turn that
  feature off in your terminal if this matters to you.
- **The VM process is only partly confined.** Landlock limits its files, not its network, and it holds
  the real values of the bound credentials. smolvm documents no seccomp filter of its own, and `Seccomp` was measured as 0 on its main thread. A kernel or KVM bug is outside what any of this covers.

The image and smolvm come with supply-chain trust you should know about:

- **Image builds use unpinned sources.** `assets/provision.sh` runs `curl | sh` installers and installs
  `npm@latest` with open egress and no cooldown. The build has no credentials, but what it installs runs
  in every sandbox.
- **`smolvm` on your `PATH` isn't checked.** It wins over the pinned copy and runs unsandboxed apart from
  the confinement above. See [Where smolvm comes from](#where-smolvm-comes-from).

## Credential boundary limits

These come from smolvm's interception on credential hosts:

- **No WebSockets or `CONNECT`** on those hosts, including `github.com` when a GitHub token is bound.
- **Request bodies over 8 MiB are refused** there. A large `git push` over HTTPS can hit this; set
  `sandbox.ssh_agent = true` and use an SSH remote if so.
- **HTTP/1.1 only** from the guest side. Responses still stream.
- A rotated secret takes effect at the next VM start (`vk stop`).

## Where smolvm comes from

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
