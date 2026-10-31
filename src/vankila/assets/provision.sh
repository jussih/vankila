#!/bin/bash
# Bakes the golden image. Runs as root in a throwaway VM with open egress and
# no credentials. Each `step` line is the progress `vk build` shows.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
step() { echo "==> $*"; }

case "$(uname -m)" in
  x86_64) NODE_ARCH=x64 ;;
  aarch64) NODE_ARCH=arm64 ;;
  *) echo "unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

step "Installing system packages"
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  bash-completion build-essential ca-certificates curl fd-find file git jq less \
  libssl-dev openssh-client pkg-config procps python3 ripgrep sudo tar unzip vim-tiny \
  wget xz-utils zip ${VK_APT:-} >/dev/null

step "Creating the agent user"
useradd --create-home --shell /bin/bash --uid 1000 agent
echo 'agent ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/agent
chmod 0440 /etc/sudoers.d/agent
ln -sf /usr/bin/fdfind /usr/local/bin/fd
install -d -o agent -g agent /workspace

step "Installing gh"
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
  -o /etc/apt/keyrings/githubcli-archive-keyring.gpg
echo "deb [signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
  >/etc/apt/sources.list.d/github-cli.list
apt-get update -qq
apt-get install -y -qq --no-install-recommends gh >/dev/null

step "Installing Node.js ${VK_NODE}"
NODE_BASE="https://nodejs.org/dist/latest-v${VK_NODE}.x"
NODE_FILE="$(curl -fsSL "$NODE_BASE/SHASUMS256.txt" | awk -v a="linux-${NODE_ARCH}.tar.xz" '$2 ~ a {print $2}')"
cd /tmp
curl -fsSLO "$NODE_BASE/$NODE_FILE"
curl -fsSL "$NODE_BASE/SHASUMS256.txt" | grep -F " $NODE_FILE" | sha256sum -c - >/dev/null
tar -xJf "$NODE_FILE" -C /usr/local --strip-components=1 --no-same-owner
rm -f "$NODE_FILE"
# min-release-age needs a recent npm; the one bundled with Node may predate it.
npm install -g --silent --no-audit --no-fund npm@latest
corepack enable

step "Installing uv"
curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_UNMANAGED_INSTALL=1 sh >/dev/null

step "Installing Rust ${VK_RUST}"
export RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo
curl -fsSL https://sh.rustup.rs | sh -s -- -y --no-modify-path --profile minimal \
  --default-toolchain "$VK_RUST" -c clippy -c rustfmt >/dev/null 2>&1
chown -R agent:agent "$RUSTUP_HOME" "$CARGO_HOME"

step "Installing Claude Code"
sudo -u agent -H bash -c 'curl -fsSL https://claude.ai/install.sh | bash' >/dev/null

step "Installing pi"
npm install -g --silent --no-audit --no-fund --ignore-scripts @earendil-works/pi-coding-agent

step "Cleaning up"
apt-get clean
rm -rf /var/lib/apt/lists/* /root/.npm /tmp/*

step "Done"
