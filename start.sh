#!/usr/bin/env bash
# ============================================================
#  WartinLabs Voice Agent – One-Click Start (Ubuntu 22.04)
#  Run:  chmod +x start.sh && ./start.sh
# ============================================================
set -e

BOLD='\033[1m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'
YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

banner() {
echo -e "${CYAN}"
cat << 'EOF'
 __        __         _   _       _           _
 \ \      / /_ _ _ __| |_(_)_ __ | |     __ _| |__  ___
  \ \ /\ / / _` | '__| __| | '_ \| |    / _` | '_ \/ __|
   \ V  V / (_| | |  | |_| | | | | |___| (_| | |_) \__ \
    \_/\_/ \__,_|_|   \__|_|_| |_|_____|\__,_|_.__/|___/

           AI Voice Agent  –  Ubuntu 22.04
EOF
echo -e "${NC}"
}

step() { echo -e "\n${GREEN}[✓] $1${NC}"; }
info() { echo -e "${CYAN}[→] $1${NC}"; }
warn() { echo -e "${YELLOW}[!] $1${NC}"; }
die()  { echo -e "${RED}[✗] $1${NC}"; exit 1; }

banner

# ── 1. System dependencies ──────────────────────────────────
step "Installing system dependencies"
sudo apt-get update -qq
sudo apt-get install -y -qq \
    python3.11 python3.11-venv python3.11-dev \
    python3-pip build-essential curl git \
    portaudio19-dev libsndfile1 ffmpeg \
    libssl-dev libffi-dev

# ── 2. Virtual environment ───────────────────────────────────
VENV_DIR="$(dirname "$0")/venv"
if [ ! -d "$VENV_DIR" ]; then
    step "Creating Python 3.11 virtual environment"
    python3.11 -m venv "$VENV_DIR"
fi

source "$VENV_DIR/bin/activate"
step "Virtual environment activated"

# ── 3. Python packages ───────────────────────────────────────
step "Installing Python packages (first run may take 3-5 min)"
pip install --upgrade pip --quiet
pip install -r "$(dirname "$0")/requirements.txt" --quiet
step "All Python packages installed"

# ── 4. Check .env ────────────────────────────────────────────
ENV_FILE="$(dirname "$0")/.env"
if [ ! -f "$ENV_FILE" ]; then
    warn ".env not found – creating from template"
    cp "$(dirname "$0")/.env.example" "$ENV_FILE"
    echo ""
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BOLD}  ACTION REQUIRED: Add your FREE API keys to .env${NC}"
    echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
    echo "  1. DEEPGRAM  → https://console.deepgram.com    (12k min/year free)"
    echo "  2. GROQ      → https://console.groq.com        (free tier)"
    echo "  3. DAILY     → https://dashboard.daily.co      (10k min/month free)"
    echo "  4. RESEND    → https://resend.com               (3k emails/month free)"
    echo ""
    echo "  Edit the file:  nano $ENV_FILE"
    echo ""
    exit 0
fi

# Check required keys are set
source "$ENV_FILE"
MISSING=()
[ -z "$DEEPGRAM_API_KEY" ] || [ "$DEEPGRAM_API_KEY" = "your_deepgram_api_key_here" ] && MISSING+=("DEEPGRAM_API_KEY")
[ -z "$GROQ_API_KEY" ]     || [ "$GROQ_API_KEY"     = "your_groq_api_key_here"     ] && MISSING+=("GROQ_API_KEY")
[ -z "$DAILY_API_KEY" ]    || [ "$DAILY_API_KEY"    = "your_daily_api_key_here"    ] && MISSING+=("DAILY_API_KEY")

if [ ${#MISSING[@]} -gt 0 ]; then
    warn "Missing API keys in .env: ${MISSING[*]}"
    echo ""
    echo "  Edit:  nano $ENV_FILE"
    echo ""
    exit 1
fi

# ── 5. Launch ────────────────────────────────────────────────
step "All checks passed – starting WartinLabs Voice Agent"
echo ""
echo -e "${CYAN}  ┌─────────────────────────────────────────┐${NC}"
echo -e "${CYAN}  │  🌐  Open: http://localhost:8000          │${NC}"
echo -e "${CYAN}  │  Press Ctrl+C to stop                     │${NC}"
echo -e "${CYAN}  └─────────────────────────────────────────┘${NC}"
echo ""

cd "$(dirname "$0")/backend"
exec python server.py
