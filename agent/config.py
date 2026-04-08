"""Configuration management for JDL Trade coding agent."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from ~/jdltrading/ first, then fall back to cwd
_here = Path(__file__).resolve().parent.parent  # ~/jdltrading
load_dotenv(_here / ".env", override=False)
load_dotenv(override=False)  # also check cwd


class Config:
    # Claude model settings
    MODEL = "claude-opus-4-6"
    MAX_TOKENS = int(os.getenv("AGENT_MAX_TOKENS", "128000"))
    EFFORT = os.getenv("AGENT_EFFORT", "max")  # low | medium | high | max

    # Paths — workspace lives inside ~/jdltrading by default
    HOME = Path.home()
    INSTALL_DIR = _here                          # ~/jdltrading
    BASE_DIR = HOME / ".jdltrade"
    MEMORY_DIR  = Path(os.getenv("AGENT_MEMORY_DIR",  str(BASE_DIR / "memory")))
    SESSION_DIR = Path(os.getenv("AGENT_SESSION_DIR", str(BASE_DIR / "sessions")))
    WORKSPACE   = Path(os.getenv("AGENT_WORKSPACE",   str(INSTALL_DIR / "workspace")))
    LOG_DIR     = BASE_DIR / "logs"

    # API Keys
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")
    BINANCE_API_KEY   = os.getenv("BINANCE_API_KEY", "")
    BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")

    # Shell settings
    SHELL_TIMEOUT = 120       # seconds per command
    MAX_OUTPUT_LENGTH = 50000  # chars before truncation

    # Tool settings
    MAX_FILE_SIZE = 1024 * 1024 * 10  # 10 MB

    @classmethod
    def ensure_dirs(cls):
        for d in [cls.MEMORY_DIR, cls.SESSION_DIR, cls.WORKSPACE, cls.LOG_DIR]:
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def validate(cls):
        if not cls.ANTHROPIC_API_KEY:
            raise ValueError(
                "ANTHROPIC_API_KEY not set.\n"
                f"  Edit {cls.INSTALL_DIR}/.env and add:\n"
                "  ANTHROPIC_API_KEY=sk-ant-..."
            )

    @classmethod
    def show(cls):
        """Print current effective config (no secrets)."""
        print(f"  Install dir : {cls.INSTALL_DIR}")
        print(f"  Workspace   : {cls.WORKSPACE}")
        print(f"  Memory      : {cls.MEMORY_DIR}")
        print(f"  Sessions    : {cls.SESSION_DIR}")
        print(f"  Model       : {cls.MODEL}")
        print(f"  Max tokens  : {cls.MAX_TOKENS}")
        print(f"  Effort      : {cls.EFFORT}")
        print(f"  API key set : {'yes' if cls.ANTHROPIC_API_KEY else 'NO — set ANTHROPIC_API_KEY'}")
