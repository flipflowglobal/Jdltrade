"""Configuration management for JDL Trade coding agent."""

import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


class Config:
    # Claude model settings
    MODEL = "claude-opus-4-6"
    MAX_TOKENS = int(os.getenv("AGENT_MAX_TOKENS", "128000"))
    EFFORT = os.getenv("AGENT_EFFORT", "max")  # low | medium | high | max

    # Paths
    HOME = Path.home()
    BASE_DIR = HOME / ".jdltrade"
    MEMORY_DIR = Path(os.getenv("AGENT_MEMORY_DIR", str(BASE_DIR / "memory")))
    SESSION_DIR = Path(os.getenv("AGENT_SESSION_DIR", str(BASE_DIR / "sessions")))
    WORKSPACE = Path(os.getenv("AGENT_WORKSPACE", str(BASE_DIR / "workspace")))
    LOG_DIR = BASE_DIR / "logs"

    # API Keys
    ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
    COINGECKO_API_KEY = os.getenv("COINGECKO_API_KEY", "")
    BINANCE_API_KEY = os.getenv("BINANCE_API_KEY", "")
    BINANCE_SECRET_KEY = os.getenv("BINANCE_SECRET_KEY", "")

    # Shell settings
    SHELL_TIMEOUT = 120  # seconds
    MAX_OUTPUT_LENGTH = 50000  # chars

    # Tool settings
    MAX_FILE_SIZE = 1024 * 1024 * 10  # 10MB

    @classmethod
    def ensure_dirs(cls):
        for d in [cls.MEMORY_DIR, cls.SESSION_DIR, cls.WORKSPACE, cls.LOG_DIR]:
            d.mkdir(parents=True, exist_ok=True)

    @classmethod
    def validate(cls):
        if not cls.ANTHROPIC_API_KEY:
            raise ValueError(
                "ANTHROPIC_API_KEY not set. Add it to .env or export it as an environment variable."
            )
