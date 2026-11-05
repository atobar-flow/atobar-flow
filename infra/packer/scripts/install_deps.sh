#!/bin/bash
set -e

# Disable unattended-upgrades to prevent dpkg locks during boot (tolerate offline systemd)
sudo systemctl disable apt-daily.service apt-daily-upgrade.service || true
sudo systemctl stop apt-daily.service apt-daily-upgrade.service || true

# Ensure universe is enabled for awscli
sudo apt-get install -y software-properties-common
sudo add-apt-repository -y universe

# Add PPA for native Chromium (bypasses snap)
sudo add-apt-repository -y ppa:xtradeb/apps

# Add GitHub CLI repository
curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg | sudo dd of=/usr/share/keyrings/githubcli-archive-keyring.gpg
sudo chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | sudo tee /etc/apt/sources.list.d/github-cli.list > /dev/null

sudo apt-get update
sudo apt-get upgrade -y

# Install core SWE tools, runtimes, and libraries (Batteries Included per #110/#116)
sudo apt-get install -y \
  git \
  curl \
  jq \
  unzip \
  python3-pip \
  python3-venv \
  python3-jwt \
  python3-cryptography \
  ripgrep \
  build-essential \
  docker.io \
  at \
  gh \
  nodejs \
  npm

# Configure docker permissions for ubuntu user
sudo usermod -aG docker ubuntu || true

# Install uv system-wide (pinned to CI version)
curl -LsSf https://astral.sh/uv/0.12.11/install.sh | env CARGO_DIST_FORCE_INSTALL_DIR=/usr/local/bin sh

# Install AWS CLI v2
ARCH=$(uname -m)
if [ "$ARCH" = "aarch64" ]; then
  AWS_URL="https://awscli.amazonaws.com/awscli-exe-linux-aarch64.zip"
else
  AWS_URL="https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip"
fi
curl -sL "$AWS_URL" -o "awscliv2.zip"
unzip -q awscliv2.zip
sudo ./aws/install
rm -rf aws awscliv2.zip

# Install Vision stack
sudo apt-get install -y tigervnc-standalone-server websockify chromium openbox xterm x11-utils xdotool scrot fonts-liberation fonts-dejavu-core
