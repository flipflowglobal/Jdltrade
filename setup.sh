#!/bin/bash
# ================================================================
# JDL Trade — Setup Script
# Works on both Termux (Android) and Linux
# Run once after cloning: bash ~/jdltrading/setup.sh
# ================================================================

set -e
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[1;33m'
CYN='\033[0;36m'; BLD='\033[1m'; RST='\033[0m'

info() { echo -e "  ${GRN}[+]${RST} $*"; }
warn() { echo -e "  ${YLW}[!]${RST} $*"; }
err()  { echo -e "  ${RED}[✗]${RST} $*"; exit 1; }
step() { echo -e "\n  ${CYN}${BLD}──── $* ────${RST}"; }

echo -e "${CYN}${BLD}"
echo "  ╔══════════════════════════════════════╗"
echo "  ║   JDL Trade · Coding Agent Setup     ║"
echo "  ║   ~/jdltrading                        ║"
echo "  ╚══════════════════════════════════════╝"
echo -e "${RST}"

# ── Detect platform ───────────────────────────────────────────────
IS_TERMUX=false
if [ -n "$PREFIX" ] && echo "$PREFIX" | grep -q "termux"; then
    IS_TERMUX=true
fi

# ── Install system packages ───────────────────────────────────────
step "System packages"
if $IS_TERMUX; then
    info "Termux detected — using pkg"
    pkg update -y -q
    pkg install -y -q python python-pip nano git curl
else
    info "Linux detected — checking tools"
    for tool in python3 pip3 nano git curl; do
        if command -v "$tool" &>/dev/null; then
            info "$tool ✓  $(command -v $tool)"
        else
            warn "$tool not found — trying apt..."
            apt-get install -y -q "$tool" 2>/dev/null || warn "Could not install $tool"
        fi
    done
fi

# ── Create runtime directories ────────────────────────────────────
step "Directories"
mkdir -p ~/.jdltrade/{memory,sessions,logs}
mkdir -p "$SCRIPT_DIR/workspace"
info "~/.jdltrade/{memory,sessions,logs}"
info "$SCRIPT_DIR/workspace"

# ── Python dependencies ───────────────────────────────────────────
step "Python dependencies"
PIP=$(command -v pip3 || command -v pip)
$PIP install -q -r "$SCRIPT_DIR/requirements.txt"
info "All packages installed"

# ── .env file ─────────────────────────────────────────────────────
step "Configuration"
ENV_FILE="$SCRIPT_DIR/.env"
if [ ! -f "$ENV_FILE" ] || ! grep -q "ANTHROPIC_API_KEY=sk-" "$ENV_FILE" 2>/dev/null; then
    cp "$SCRIPT_DIR/.env.example" "$ENV_FILE" 2>/dev/null || true
    warn ".env created — you MUST add your ANTHROPIC_API_KEY:"
    warn "  nano $ENV_FILE"
else
    info ".env already configured ✓"
fi

# ── Permissions ───────────────────────────────────────────────────
chmod +x "$SCRIPT_DIR/nano_builder.sh"
chmod +x "$SCRIPT_DIR/run.sh" 2>/dev/null || true

# ── Verify imports ────────────────────────────────────────────────
step "Verifying Python imports"
python3 -c "import anthropic, rich, prompt_toolkit, httpx, dotenv; print('  All imports OK')"

echo ""
echo -e "  ${GRN}${BLD}╔══════════════════════════════════════╗${RST}"
echo -e "  ${GRN}${BLD}║  Setup complete!                     ║${RST}"
echo -e "  ${GRN}${BLD}╚══════════════════════════════════════╝${RST}"
echo ""
echo -e "  Next steps:"
echo -e "  ${YLW}1.${RST} Add your API key:  nano $ENV_FILE"
echo -e "  ${YLW}2.${RST} Start the agent:   cd ~/jdltrading && python3 main.py"
echo -e "  ${YLW}3.${RST} Build a project:   bash nano_builder.sh"
echo ""
