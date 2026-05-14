#!/usr/bin/env bash
# init.sh — Oracle Cloud Always Free ARM64 (Ubuntu 24.04) bootstrapper for fowardBot
# Run as root or via sudo: sudo bash init.sh
set -euo pipefail

# ── colours ──────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()     { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

# ── must run as root ──────────────────────────────────────────────────────────
[[ $EUID -eq 0 ]] || die "Run as root: sudo bash $0"

# ── config ────────────────────────────────────────────────────────────────────
BOT_USER="${SUDO_USER:-ubuntu}"          # the non-root account that will run the bot
BOT_DIR="/home/${BOT_USER}/fowardBot"   # where the project lives on the host
SWAP_FILE="/swapfile"
SWAP_SIZE="2G"                           # 2 GB swap — safety buffer on a 6 GB instance

# ── 1. system update ──────────────────────────────────────────────────────────
info "Updating package lists and upgrading installed packages..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq

# ── 2. essential tools ────────────────────────────────────────────────────────
info "Installing essential packages..."
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    ca-certificates curl gnupg git htop unzip ufw fail2ban \
    unattended-upgrades apt-listchanges

# ── 3. Docker ─────────────────────────────────────────────────────────────────
if command -v docker &>/dev/null; then
    info "Docker already installed ($(docker --version)), skipping."
else
    info "Installing Docker CE from the official repository..."
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
        | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    chmod a+r /etc/apt/keyrings/docker.gpg

    echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/ubuntu \
        $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
        > /etc/apt/sources.list.d/docker.list

    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
        docker-ce docker-ce-cli containerd.io \
        docker-buildx-plugin docker-compose-plugin

    info "Docker $(docker --version) installed."
fi

# ── 4. Docker daemon configuration ────────────────────────────────────────────
info "Configuring Docker daemon (log rotation + live-restore)..."
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
systemctl enable docker --quiet
systemctl restart docker

# ── 5. add bot user to docker group ───────────────────────────────────────────
if id "$BOT_USER" &>/dev/null; then
    info "Adding $BOT_USER to the docker group..."
    usermod -aG docker "$BOT_USER"
    warn "Log out and back in as '$BOT_USER' for the group change to take effect,"
    warn "or prefix docker commands with 'sudo' in this session."
else
    warn "User '$BOT_USER' not found — skipping docker group assignment."
fi

# ── 6. swap ───────────────────────────────────────────────────────────────────
if [[ -f "$SWAP_FILE" ]]; then
    info "Swap file already exists at $SWAP_FILE, skipping."
else
    info "Creating ${SWAP_SIZE} swap file at ${SWAP_FILE}..."
    fallocate -l "$SWAP_SIZE" "$SWAP_FILE"
    chmod 600 "$SWAP_FILE"
    mkswap "$SWAP_FILE" -q
    swapon "$SWAP_FILE"
    echo "$SWAP_FILE none swap sw 0 0" >> /etc/fstab
    info "Swap enabled. Current usage:"
    swapon --show
fi

# ── 7. firewall ───────────────────────────────────────────────────────────────
info "Configuring UFW firewall..."
ufw --force reset > /dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow ssh comment 'SSH'
# The bot has no inbound ports — all Telegram traffic is outbound MTProto.
ufw --force enable
info "UFW status:"
ufw status verbose

# ── 8. fail2ban (brute-force SSH protection) ──────────────────────────────────
info "Enabling fail2ban for SSH protection..."
systemctl enable fail2ban --quiet
systemctl start fail2ban

# ── 9. unattended security upgrades ──────────────────────────────────────────
info "Enabling unattended security upgrades..."
cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
APT::Periodic::AutocleanInterval "7";
EOF
systemctl enable unattended-upgrades --quiet
systemctl start unattended-upgrades

# ── 10. project directory ─────────────────────────────────────────────────────
if [[ -d "$BOT_DIR" ]]; then
    info "Project directory $BOT_DIR already exists, skipping creation."
else
    info "Creating project directory $BOT_DIR..."
    mkdir -p "$BOT_DIR"
    chown "$BOT_USER:$BOT_USER" "$BOT_DIR"
fi

# ── done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN} Bootstrap complete.${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo " Next steps (run as '${BOT_USER}', not root):"
echo ""
echo "   1. Re-login or run:  newgrp docker"
echo ""
echo "   2. Copy your project files to the instance:"
echo "      scp -r ./fowardBot ubuntu@<instance-ip>:~/"
echo "      # or clone from your git repository:"
echo "      git clone <your-repo-url> ~/fowardBot"
echo ""
echo "   3. Set up credentials:"
echo "      cd ~/fowardBot"
echo "      cp .env.example .env"
echo "      nano .env   # fill in API_ID and API_HASH"
echo ""
echo "   4. Build the image:"
echo "      docker compose build"
echo ""
echo "   5. First-run authentication (required once):"
echo "      docker compose run --rm forwardbot"
echo ""
echo "   6. Start detached:"
echo "      docker compose up -d"
echo "      docker compose logs -f forwardbot"
echo ""