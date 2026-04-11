"""
Tool implementations for JDL Trade coding agent.
All tools are designed for use with the Claude API tool-use protocol.
"""

import os
import subprocess
import json
import time
import traceback
from pathlib import Path
from typing import Any

import httpx

from .config import Config


# ─────────────────────────────────────────────
# Tool Definitions (JSON Schema for Claude API)
# ─────────────────────────────────────────────

TOOL_DEFINITIONS = [
    {
        "name": "shell",
        "description": (
            "Execute any shell command in the Termux/Linux environment. "
            "Use this to: install packages (pip install / pkg install / apt install), "
            "run Python scripts, compile code, query the filesystem, run git commands, "
            "start/stop processes, test code, fetch system info. "
            "Supports multi-line bash scripts. Working directory persists between calls. "
            "ALWAYS use this to actually run and test code you write."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command or bash script to execute. Can be multi-line.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default: 120, max: 600). Use higher values for long builds/installs.",
                    "default": 120,
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory for the command. Defaults to the agent workspace.",
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Write content to a file, creating parent directories as needed. "
            "Use for: creating source files, configs, scripts, data files. "
            "Overwrites existing files. For partial edits, use shell with sed/awk "
            "or read first and write the modified version."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path. Use absolute paths or paths relative to workspace.",
                },
                "content": {
                    "type": "string",
                    "description": "Full file content to write.",
                },
                "mode": {
                    "type": "string",
                    "description": "Write mode: 'w' (overwrite, default) or 'a' (append).",
                    "default": "w",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read the contents of a file. Use before editing to understand current state. "
            "Returns file content with line numbers for easy reference."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to read.",
                },
                "start_line": {
                    "type": "integer",
                    "description": "Starting line number (1-indexed). Reads from beginning if not specified.",
                },
                "end_line": {
                    "type": "integer",
                    "description": "Ending line number (inclusive). Reads to end if not specified.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_files",
        "description": (
            "List files and directories at a path. "
            "Returns a tree-style listing with file sizes and types."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list. Defaults to workspace.",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "List recursively (default: false). Use with caution on large trees.",
                    "default": False,
                },
                "show_hidden": {
                    "type": "boolean",
                    "description": "Include hidden files/dirs (starting with .). Default: false.",
                    "default": False,
                },
            },
            "required": [],
        },
    },
    {
        "name": "web_fetch",
        "description": (
            "Fetch content from a URL. Use for: reading documentation, fetching API data, "
            "getting blockchain data from REST APIs, reading GitHub raw files. "
            "Returns the response body as text. Supports custom headers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "The URL to fetch.",
                },
                "method": {
                    "type": "string",
                    "description": "HTTP method: GET (default), POST, PUT, DELETE.",
                    "default": "GET",
                },
                "headers": {
                    "type": "object",
                    "description": "HTTP headers as key-value pairs.",
                },
                "body": {
                    "type": "string",
                    "description": "Request body for POST/PUT (as JSON string or raw text).",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Request timeout in seconds (default: 30).",
                    "default": 30,
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "memory_write",
        "description": (
            "Persist important information to long-term memory. "
            "Use for: saving project structure, API keys patterns, architectural decisions, "
            "progress checkpoints, credentials patterns, discovered bugs and fixes. "
            "Memory persists across sessions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Unique identifier for this memory (e.g., 'project_structure', 'api_endpoints').",
                },
                "content": {
                    "type": "string",
                    "description": "Content to store in memory.",
                },
                "append": {
                    "type": "boolean",
                    "description": "Append to existing memory for this key (default: false = overwrite).",
                    "default": False,
                },
            },
            "required": ["key", "content"],
        },
    },
    {
        "name": "memory_read",
        "description": (
            "Read from long-term memory. Use to recall: previous project context, "
            "saved configurations, architectural notes, checkpoints."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "key": {
                    "type": "string",
                    "description": "Memory key to read. Use '*' to list all available keys.",
                },
            },
            "required": ["key"],
        },
    },
    {
        "name": "crypto_price",
        "description": (
            "Get real-time cryptocurrency price data from CoinGecko. "
            "Returns current price, 24h change, market cap, volume for one or more tokens. "
            "Use for: sanity-checking prices, calibrating trading systems, market analysis."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "coins": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "CoinGecko coin IDs (e.g., ['bitcoin', 'ethereum', 'solana']).",
                },
                "vs_currency": {
                    "type": "string",
                    "description": "Quote currency (default: 'usd').",
                    "default": "usd",
                },
            },
            "required": ["coins"],
        },
    },
]


# ─────────────────────────────────────────────
# Tool Executor
# ─────────────────────────────────────────────

class ToolExecutor:
    """Executes tool calls from Claude and returns results."""

    def __init__(self):
        Config.ensure_dirs()
        self._shell_cwd = str(Config.WORKSPACE)

    def execute(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        try:
            handler = getattr(self, f"_tool_{tool_name}", None)
            if handler is None:
                return f"[ERROR] Unknown tool: {tool_name}"
            return handler(**tool_input)
        except Exception as e:
            tb = traceback.format_exc()
            return f"[ERROR] Tool '{tool_name}' raised exception:\n{e}\n\nTraceback:\n{tb}"

    # ── Shell ──────────────────────────────────

    def _tool_shell(self, command: str, timeout: int = 120, cwd: str | None = None) -> str:
        timeout = min(timeout, 600)
        work_dir = cwd or self._shell_cwd

        # Ensure working directory exists
        Path(work_dir).mkdir(parents=True, exist_ok=True)

        try:
            result = subprocess.run(
                command,
                shell=True,
                executable="/bin/bash",
                capture_output=True,
                text=True,
                timeout=timeout,
                cwd=work_dir,
            )
        except subprocess.TimeoutExpired:
            return f"[TIMEOUT] Command timed out after {timeout}s:\n{command[:200]}"

        output_parts = []
        if result.stdout:
            output_parts.append(f"STDOUT:\n{result.stdout}")
        if result.stderr:
            output_parts.append(f"STDERR:\n{result.stderr}")

        exit_label = "SUCCESS" if result.returncode == 0 else f"FAILED (exit {result.returncode})"
        combined = "\n".join(output_parts) if output_parts else "(no output)"

        # Truncate very long outputs
        if len(combined) > Config.MAX_OUTPUT_LENGTH:
            combined = (
                combined[: Config.MAX_OUTPUT_LENGTH // 2]
                + "\n\n... [OUTPUT TRUNCATED] ...\n\n"
                + combined[-Config.MAX_OUTPUT_LENGTH // 4 :]
            )

        return f"[{exit_label}]\n{combined}"

    # ── File Operations ────────────────────────

    def _tool_write_file(self, path: str, content: str, mode: str = "w") -> str:
        p = self._resolve_path(path)
        if p.stat().st_size > Config.MAX_FILE_SIZE if p.exists() else False:
            return f"[ERROR] Target file too large (>10MB): {p}"

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8") if mode == "w" else open(p, "a").write(content)

        lines = content.count("\n") + 1
        return f"[OK] Wrote {len(content)} bytes ({lines} lines) to {p}"

    def _tool_read_file(
        self, path: str, start_line: int | None = None, end_line: int | None = None
    ) -> str:
        p = self._resolve_path(path)
        if not p.exists():
            return f"[ERROR] File not found: {p}"
        if p.stat().st_size > Config.MAX_FILE_SIZE:
            return f"[ERROR] File too large to read directly (>10MB). Use shell with head/tail/grep."

        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        total = len(lines)

        s = (start_line - 1) if start_line else 0
        e = end_line if end_line else total
        s, e = max(0, s), min(total, e)

        selected = lines[s:e]
        numbered = "\n".join(f"{s+i+1:5d} | {l}" for i, l in enumerate(selected))
        return f"File: {p}  ({total} total lines, showing {s+1}-{e})\n{'─'*60}\n{numbered}"

    def _tool_list_files(
        self, path: str | None = None, recursive: bool = False, show_hidden: bool = False
    ) -> str:
        p = self._resolve_path(path or str(Config.WORKSPACE))
        if not p.exists():
            return f"[ERROR] Path not found: {p}"
        if not p.is_dir():
            return f"[ERROR] Not a directory: {p}"

        lines = [f"Directory: {p}\n{'─'*60}"]

        def _fmt(entry: Path, indent: int = 0) -> str:
            prefix = "  " * indent
            if entry.is_symlink():
                return f"{prefix}⟶  {entry.name} → {entry.readlink()}"
            elif entry.is_dir():
                return f"{prefix}📁 {entry.name}/"
            else:
                size = entry.stat().st_size
                size_str = f"{size:,}B" if size < 1024 else f"{size//1024:,}KB"
                return f"{prefix}📄 {entry.name}  ({size_str})"

        def _list(dir_: Path, indent: int = 0):
            try:
                entries = sorted(dir_.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
            except PermissionError:
                lines.append("  " * indent + "[Permission denied]")
                return
            for entry in entries:
                if not show_hidden and entry.name.startswith("."):
                    continue
                lines.append(_fmt(entry, indent))
                if recursive and entry.is_dir() and not entry.is_symlink():
                    _list(entry, indent + 1)

        _list(p)
        return "\n".join(lines)

    # ── Web Fetch ──────────────────────────────

    def _tool_web_fetch(
        self,
        url: str,
        method: str = "GET",
        headers: dict | None = None,
        body: str | None = None,
        timeout: int = 30,
    ) -> str:
        h = {"User-Agent": "JDLTradeAgent/1.0"}
        if headers:
            h.update(headers)

        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                response = client.request(
                    method.upper(),
                    url,
                    headers=h,
                    content=body.encode() if body else None,
                )
            text = response.text
            if len(text) > 20000:
                text = text[:20000] + "\n\n... [TRUNCATED at 20k chars] ..."
            return (
                f"Status: {response.status_code}\n"
                f"Content-Type: {response.headers.get('content-type', 'unknown')}\n"
                f"{'─'*60}\n"
                f"{text}"
            )
        except httpx.TimeoutException:
            return f"[ERROR] Request timed out after {timeout}s: {url}"
        except Exception as e:
            return f"[ERROR] HTTP request failed: {e}"

    # ── Memory ─────────────────────────────────

    def _tool_memory_write(self, key: str, content: str, append: bool = False) -> str:
        safe_key = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
        path = Config.MEMORY_DIR / f"{safe_key}.md"
        mode = "a" if append else "w"
        with open(path, mode, encoding="utf-8") as f:
            if append:
                f.write(f"\n\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n{content}")
            else:
                f.write(f"# {key}\nUpdated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n{content}")
        return f"[OK] Memory saved: {safe_key}"

    def _tool_memory_read(self, key: str) -> str:
        if key == "*":
            files = list(Config.MEMORY_DIR.glob("*.md"))
            if not files:
                return "No memory entries found."
            keys = [f.stem for f in sorted(files)]
            return "Available memory keys:\n" + "\n".join(f"  • {k}" for k in keys)

        safe_key = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
        path = Config.MEMORY_DIR / f"{safe_key}.md"
        if not path.exists():
            return f"[NOT FOUND] No memory entry for key: {key}"
        return path.read_text(encoding="utf-8")

    # ── Crypto Price ───────────────────────────

    def _tool_crypto_price(self, coins: list[str], vs_currency: str = "usd") -> str:
        ids = ",".join(coins)
        url = (
            f"https://api.coingecko.com/api/v3/simple/price"
            f"?ids={ids}&vs_currencies={vs_currency}"
            f"&include_24hr_change=true&include_market_cap=true&include_24hr_vol=true"
        )
        headers = {}
        if Config.COINGECKO_API_KEY:
            headers["x-cg-demo-api-key"] = Config.COINGECKO_API_KEY

        try:
            with httpx.Client(timeout=10) as client:
                r = client.get(url, headers=headers)
                r.raise_for_status()
                data = r.json()
        except Exception as e:
            return f"[ERROR] CoinGecko fetch failed: {e}"

        if not data:
            return f"[ERROR] No data returned. Check coin IDs: {coins}"

        lines = [f"Crypto Prices ({vs_currency.upper()}) — {time.strftime('%Y-%m-%d %H:%M:%S UTC')}"]
        lines.append("─" * 60)
        for coin, info in data.items():
            price = info.get(vs_currency, "N/A")
            change = info.get(f"{vs_currency}_24h_change", 0)
            mcap = info.get(f"{vs_currency}_market_cap", 0)
            vol = info.get(f"{vs_currency}_24h_vol", 0)
            arrow = "▲" if change and change > 0 else "▼"
            lines.append(
                f"  {coin.upper():12s}  ${price:>15,.4f}  "
                f"{arrow} {abs(change or 0):.2f}%  "
                f"MCap: ${mcap/1e9:.1f}B  Vol: ${vol/1e6:.0f}M"
            )
        return "\n".join(lines)

    # ── Helpers ────────────────────────────────

    def _resolve_path(self, path: str) -> Path:
        p = Path(path)
        if not p.is_absolute():
            p = Config.WORKSPACE / p
        return p.expanduser().resolve()
