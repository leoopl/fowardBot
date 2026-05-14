#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# Oracle Cloud Always Free — Ubuntu 24.04 aarch64 (VM.Standard.A1.Flex)
#
# Alternative to cloud-init.yml for the "Initialization script" field.
# Paste this file content in the "Paste cloud-init script" option under
# Advanced Options when creating a new instance. Runs once as root during
# first boot, before you ever SSH in.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
exec > /var/log/cloud-init-fowardbot.log 2>&1

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Starting fowardBot cloud-init setup..."

# ── 1. system update ──────────────────────────────────────────────────────────
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq

# ── 2. base packages ──────────────────────────────────────────────────────────
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    ca-certificates curl gnupg git htop \
    ufw fail2ban unattended-upgrades apt-listchanges

# ── 3. Docker CE ──────────────────────────────────────────────────────────────
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc

. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list

apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    docker-ce docker-ce-cli containerd.io \
    docker-buildx-plugin docker-compose-plugin

systemctl enable docker
systemctl start docker
usermod -aG docker ubuntu

# ── 4. Docker daemon config ───────────────────────────────────────────────────
mkdir -p /etc/docker
cat > /etc/docker/daemon.json <<'EOF'
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "20m",
    "max-file": "5"
  },
  "live-restore": true
}
EOF
systemctl restart docker

# ── 5. swap (2 GB) ────────────────────────────────────────────────────────────
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo "/swapfile none swap sw 0 0" >> /etc/fstab

# ── 6. UFW firewall ───────────────────────────────────────────────────────────
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh comment 'SSH'
ufw --force enable

# ── 7. fail2ban ───────────────────────────────────────────────────────────────
cat > /etc/fail2ban/jail.local <<'EOF'
[sshd]
enabled  = true
port     = ssh
maxretry = 5
bantime  = 3600
findtime = 600
EOF
systemctl enable fail2ban
systemctl start fail2ban

# ── 8. unattended security upgrades ──────────────────────────────────────────
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
systemctl enable unattended-upgrades
systemctl start unattended-upgrades

# ── 9. project directory ──────────────────────────────────────────────────────
mkdir -p /home/ubuntu/fowardBot
chown ubuntu:ubuntu /home/ubuntu/fowardBot

# ── done marker ───────────────────────────────────────────────────────────────
echo "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > /home/ubuntu/.cloud-init-done
chown ubuntu:ubuntu /home/ubuntu/.cloud-init-done

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] fowardBot cloud-init setup complete."
