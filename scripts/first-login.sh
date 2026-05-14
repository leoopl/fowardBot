#!/usr/bin/env bash
# first-login.sh — run once, manually, after your first SSH into the instance.
# It guides you through copying the project, setting credentials, building the
# image, and authenticating with Telegram.
#
# Usage:  bash first-login.sh
set -euo pipefail

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[✔]${NC} $*"; }
step()  { echo -e "${CYAN}[→]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
die()   { echo -e "${RED}[✘]${NC} $*" >&2; exit 1; }
ask()   { echo -e "${CYAN}[?]${NC} $*"; }

BOT_DIR="$HOME/fowardBot"

echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}  fowardBot — first-login setup${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# ── 1. verify cloud-init finished ────────────────────────────────────────────
step "Checking cloud-init completed..."
if [[ ! -f "$HOME/.cloud-init-done" ]]; then
    warn "Cloud-init marker not found at ~/.cloud-init-done"
    warn "The instance may still be provisioning. Check with:"
    warn "  sudo cloud-init status --wait"
    read -rp "    Continue anyway? [y/N] " REPLY
    [[ "${REPLY,,}" == "y" ]] || { echo "Aborting. Try again once cloud-init finishes."; exit 1; }
else
    info "Cloud-init completed at $(cat "$HOME/.cloud-init-done")"
fi

# ── 2. verify Docker ──────────────────────────────────────────────────────────
step "Checking Docker..."
if ! command -v docker &>/dev/null; then
    die "Docker not found. Did cloud-init complete successfully?"
fi
if ! docker info &>/dev/null 2>&1; then
    warn "Cannot reach Docker daemon. Your user may not be in the docker group yet."
    warn "Run: newgrp docker  (or log out and back in)"
    die "Fix docker group membership, then re-run this script."
fi
info "Docker $(docker version --format '{{.Server.Version}}') — OK"
info "Docker Compose $(docker compose version --short) — OK"

# ── 3. project files ──────────────────────────────────────────────────────────
step "Checking project files at $BOT_DIR..."
REQUIRED_FILES=(main.py handlers.py config_store.py matcher.py docker-compose.yml Dockerfile requirements.txt .env.example)
MISSING=()
for f in "${REQUIRED_FILES[@]}"; do
    [[ -f "$BOT_DIR/$f" ]] || MISSING+=("$f")
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
    warn "Missing files in $BOT_DIR: ${MISSING[*]}"
    echo ""
    echo "  Copy the project from your local machine:"
    echo -e "    ${YELLOW}scp -r ./fowardBot ubuntu@<instance-ip>:~/${NC}"
    echo ""
    echo "  Or clone from git:"
    echo -e "    ${YELLOW}git clone <your-repo-url> $BOT_DIR${NC}"
    echo ""
    read -rp "  Press Enter once files are in place, or Ctrl-C to abort: "
    for f in "${REQUIRED_FILES[@]}"; do
        [[ -f "$BOT_DIR/$f" ]] || die "Still missing: $BOT_DIR/$f"
    done
fi
info "Project files present."

cd "$BOT_DIR"

# ── 4. .env setup ─────────────────────────────────────────────────────────────
step "Setting up .env..."
if [[ -f .env ]]; then
    info ".env already exists, skipping creation."
else
    cp .env.example .env
    info "Created .env from .env.example"
fi

# Prompt for missing values
source_env() { set -a; source .env 2>/dev/null || true; set +a; }
source_env

if [[ -z "${API_ID:-}" ]]; then
    echo ""
    echo "  You need a Telegram API_ID and API_HASH."
    echo "  Get them at: https://my.telegram.org → API development tools"
    echo ""
    ask "Enter your API_ID (integer):"
    read -rp "    API_ID: " INPUT_API_ID
    [[ "$INPUT_API_ID" =~ ^[0-9]+$ ]] || die "API_ID must be a number."
    sed -i "s/^API_ID=.*/API_ID=${INPUT_API_ID}/" .env
fi

source_env
if [[ -z "${API_HASH:-}" ]]; then
    ask "Enter your API_HASH (32-char hex):"
    read -rp "    API_HASH: " INPUT_API_HASH
    [[ "${#INPUT_API_HASH}" -eq 32 ]] || die "API_HASH must be exactly 32 characters."
    sed -i "s/^API_HASH=.*/API_HASH=${INPUT_API_HASH}/" .env
fi

source_env
info "Credentials stored in .env"

# ── 5. build ──────────────────────────────────────────────────────────────────
step "Building Docker image (this may take a few minutes the first time)..."
docker compose build
info "Image built."

# ── 6. first-run authentication ───────────────────────────────────────────────
echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${CYAN}  Telegram authentication${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  Pyrogram will ask for your phone number and a confirmation code"
echo "  sent to your Telegram app. If you have 2FA enabled, it will also"
echo "  ask for your password."
echo ""
echo "  When the bot starts successfully (you'll see log output), press"
echo "  Ctrl-C to stop this interactive container. The session file is"
echo "  saved to a Docker volume and will be reused on every future start."
echo ""
read -rp "  Press Enter to begin authentication: "

docker compose run --rm forwardbot

# ── 7. start detached ─────────────────────────────────────────────────────────
echo ""
step "Starting the bot in the background..."
docker compose up -d
info "Bot is running."

echo ""
step "Waiting 10 seconds for the bot to initialise..."
sleep 10
echo ""
echo "  Recent logs:"
docker compose logs --tail=20 forwardbot

echo ""
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}  Setup complete. fowardBot is running.${NC}"
echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
echo "  Useful commands:"
echo "    docker compose logs -f forwardbot     # follow live logs"
echo "    docker compose ps                     # container status + health"
echo "    docker compose restart forwardbot     # restart the bot"
echo "    docker compose down                   # stop and remove container"
echo ""
echo "  Configure the bot from your Telegram Saved Messages chat:"
echo "    /add_keyword <phrase>"
echo "    /add_chat <id or @username>"
echo "    /list_keywords"
echo "    /list_chats"
echo ""
