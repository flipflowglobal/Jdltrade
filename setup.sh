#!/data/data/com.termux/files/usr/bin/bash
# ============================================================
# JDL Trade - Termux Coding Agent Setup
# Installs all dependencies for the advanced crypto coding agent
# ============================================================

set -e

echo "========================================"
echo "  JDL Trade Termux Coding Agent Setup  "
echo "========================================"

# Update and upgrade Termux packages
echo "[*] Updating Termux packages..."
pkg update -y && pkg upgrade -y

# Install system dependencies
echo "[*] Installing system dependencies..."
pkg install -y python python-pip git curl wget openssl libffi

# Install Python dependencies
echo "[*] Installing Python dependencies..."
pip install --upgrade pip
pip install -r requirements.txt

# Create config directory
echo "[*] Creating config directories..."
mkdir -p ~/.jdltrade/{memory,sessions,workspace,logs}

# Set up .env if it doesn't exist
if [ ! -f .env ]; then
    echo "[*] Creating .env template..."
    cat > .env << 'EOF'
# JDL Trade Coding Agent Configuration
ANTHROPIC_API_KEY=your_api_key_here

# Agent settings
AGENT_MAX_TOKENS=128000
AGENT_EFFORT=max
AGENT_MEMORY_DIR=~/.jdltrade/memory
AGENT_SESSION_DIR=~/.jdltrade/sessions
AGENT_WORKSPACE=~/.jdltrade/workspace

# Optional: crypto API keys
COINGECKO_API_KEY=
BINANCE_API_KEY=
BINANCE_SECRET_KEY=
EOF
    echo "[!] Edit .env and add your ANTHROPIC_API_KEY before running"
fi

echo ""
echo "========================================"
echo "  Setup complete!"
echo "  1. Edit .env with your API key"
echo "  2. Run: python main.py"
echo "========================================"
