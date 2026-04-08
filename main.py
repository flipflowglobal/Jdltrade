#!/usr/bin/env python3
"""
JDL Trade — Advanced Termux Coding Agent
Powered by Claude Opus 4.6 with Adaptive Thinking

Usage:
    python main.py              # Interactive REPL
    python main.py "task here"  # Single-shot query
    python main.py --resume <session_id>  # Resume a session
    python main.py --sessions   # List saved sessions
"""

import sys
import os
import argparse
import json

# ── Try to import rich for beautiful output ──────────────────────
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    from rich.markdown import Markdown
    from rich.prompt import Prompt
    from rich.live import Live
    from rich.spinner import Spinner
    from rich.table import Table
    from rich import box
    RICH = True
    console = Console()
except ImportError:
    RICH = False
    class _Console:
        def print(self, *args, **kwargs): print(*args)
        def rule(self, *args, **kwargs): print("─" * 60)
    console = _Console()


def print_banner():
    if RICH:
        banner = """[bold cyan]
     ██╗██████╗ ██╗      ████████╗██████╗  █████╗ ██████╗ ███████╗
     ██║██╔══██╗██║      ╚══██╔══╝██╔══██╗██╔══██╗██╔══██╗██╔════╝
     ██║██║  ██║██║         ██║   ██████╔╝███████║██║  ██║█████╗
██   ██║██║  ██║██║         ██║   ██╔══██╗██╔══██║██║  ██║██╔══╝
╚█████╔╝██████╔╝███████╗    ██║   ██║  ██║██║  ██║██████╔╝███████╗
 ╚════╝ ╚═════╝ ╚══════╝    ╚═╝   ╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚══════╝[/bold cyan]
[bold yellow]         Advanced Crypto Coding Agent — Termux Edition[/bold yellow]
[dim]         Powered by Claude Opus 4.6 · Adaptive Thinking · Max Effort[/dim]"""
        console.print(banner)
    else:
        print("=" * 60)
        print("  JDL TRADE — Advanced Coding Agent")
        print("  Crypto Systems | Claude Opus 4.6")
        print("=" * 60)


def format_usage(usage: dict) -> str:
    cached_pct = ""
    if usage.get("cached", 0) > 0:
        total = usage["input"] + usage["cached"]
        pct = usage["cached"] / total * 100
        cached_pct = f"  cache: {pct:.0f}%"
    return (
        f"in:{usage.get('input',0):,}  out:{usage.get('output',0):,}"
        f"{cached_pct}  |  total in:{usage.get('total_in',0):,}"
    )


def run_agent(agent, prompt: str, verbose: bool = False):
    """Run the agent on a prompt, rendering output to terminal."""
    thinking_buffer = []
    text_buffer = []
    current_tool = None
    in_thinking = False

    if RICH:
        console.rule("[dim]Agent Response[/dim]")
    else:
        print("\n" + "─" * 60)

    for event_type, data in agent.query(prompt):

        if event_type == "thinking":
            if not in_thinking and verbose:
                if RICH:
                    console.print("\n[dim italic]💭 Thinking...[/dim italic]")
                else:
                    print("\n[Thinking...]")
            in_thinking = True
            if verbose:
                if RICH:
                    console.print(f"[dim]{data}[/dim]", end="")
                else:
                    print(data, end="", flush=True)

        elif event_type == "text":
            in_thinking = False
            if RICH:
                console.print(data, end="")
            else:
                print(data, end="", flush=True)
            text_buffer.append(data)

        elif event_type == "tool_call_start":
            if RICH:
                console.print(f"\n[bold yellow]⚙  Calling tool: {data['name']}[/bold yellow]")
            else:
                print(f"\n[Tool: {data['name']}]")

        elif event_type == "tool_input":
            pass  # Streaming tool input JSON — skip display

        elif event_type == "tool_call":
            name = data["name"]
            inp = data["input"]
            current_tool = name
            if RICH:
                # Show key parts of the tool input
                summary = _summarize_tool_input(name, inp)
                console.print(f"[yellow]  → {summary}[/yellow]")
            else:
                print(f"  Input: {json.dumps(inp)[:200]}")

        elif event_type == "tool_result":
            result = data["result"]
            if RICH:
                # Show first few lines of result
                lines = result.split("\n")
                preview = "\n".join(lines[:8])
                if len(lines) > 8:
                    preview += f"\n  [dim]... +{len(lines)-8} more lines[/dim]"
                console.print(f"[green]  ← {preview}[/green]\n")
            else:
                print(f"  Result: {result[:500]}\n")

        elif event_type == "usage":
            if RICH:
                console.print(f"\n[dim]Tokens: {format_usage(data)}[/dim]")
            else:
                print(f"\n[Tokens: {format_usage(data)}]")

        elif event_type == "error":
            if RICH:
                console.print(f"\n[bold red]ERROR: {data}[/bold red]")
            else:
                print(f"\nERROR: {data}")

        elif event_type == "done":
            if RICH:
                console.rule("[dim]Done[/dim]")
            else:
                print("\n" + "─" * 60)


def _summarize_tool_input(tool_name: str, inp: dict) -> str:
    """Create a human-readable summary of a tool call."""
    if tool_name == "shell":
        cmd = inp.get("command", "")
        return f"`{cmd[:80]}`"
    elif tool_name == "write_file":
        path = inp.get("path", "?")
        lines = inp.get("content", "").count("\n") + 1
        return f"write {path} ({lines} lines)"
    elif tool_name == "read_file":
        return f"read {inp.get('path', '?')}"
    elif tool_name == "web_fetch":
        return f"fetch {inp.get('url', '?')[:60]}"
    elif tool_name == "memory_write":
        return f"save memory: {inp.get('key', '?')}"
    elif tool_name == "memory_read":
        return f"load memory: {inp.get('key', '?')}"
    elif tool_name == "crypto_price":
        return f"price {inp.get('coins', [])}"
    elif tool_name == "list_files":
        return f"ls {inp.get('path', 'workspace')}"
    return str(inp)[:80]


def interactive_repl(agent, verbose: bool = False):
    """Run the interactive REPL loop."""
    print_banner()

    if RICH:
        console.print("\n[dim]Commands: /reset (new session) | /sessions (history) | "
                      "/verbose (toggle thinking) | /exit[/dim]\n")
    else:
        print("\nCommands: /reset | /sessions | /verbose | /exit\n")

    while True:
        try:
            if RICH:
                prompt_text = Prompt.ask("[bold cyan]You[/bold cyan]")
            else:
                prompt_text = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            if RICH:
                console.print("\n[dim]Interrupted. Use /exit to quit.[/dim]")
            else:
                print("\nInterrupted. Use /exit to quit.")
            continue

        if not prompt_text:
            continue

        # ── Handle commands ──────────────────────────────
        if prompt_text.startswith("/"):
            cmd = prompt_text.lower().strip()

            if cmd in ("/exit", "/quit", "/q"):
                if RICH:
                    console.print("[dim]Goodbye. Session saved.[/dim]")
                else:
                    print("Goodbye.")
                break

            elif cmd == "/reset":
                agent.reset()
                if RICH:
                    console.print("[green]Session reset.[/green]")
                else:
                    print("Session reset.")

            elif cmd == "/verbose":
                verbose = not verbose
                state = "ON" if verbose else "OFF"
                if RICH:
                    console.print(f"[dim]Verbose thinking display: {state}[/dim]")
                else:
                    print(f"Verbose: {state}")

            elif cmd == "/sessions":
                sessions = agent.list_sessions()
                if RICH:
                    t = Table(title="Saved Sessions", box=box.ROUNDED)
                    t.add_column("Session ID", style="cyan")
                    t.add_column("Turns", justify="right")
                    t.add_column("Messages", justify="right")
                    for s in sessions[:20]:
                        t.add_row(s["id"], str(s["turns"]), str(s["messages"]))
                    console.print(t)
                else:
                    for s in sessions[:20]:
                        print(f"  {s['id']}  turns={s['turns']}  msgs={s['messages']}")

            elif cmd.startswith("/load "):
                sid = cmd[6:].strip()
                try:
                    from agent.core import JDLAgent
                    loaded = JDLAgent.load_session(sid)
                    agent.messages = loaded.messages
                    agent.session_id = loaded.session_id
                    agent.turn_count = loaded.turn_count
                    if RICH:
                        console.print(f"[green]Loaded session: {sid}[/green]")
                    else:
                        print(f"Loaded: {sid}")
                except Exception as e:
                    if RICH:
                        console.print(f"[red]Load failed: {e}[/red]")
                    else:
                        print(f"Error: {e}")

            elif cmd == "/help":
                help_text = """
/reset          — Start a new conversation
/verbose        — Toggle display of Claude's thinking process
/sessions       — List saved sessions
/load <id>      — Resume a previous session
/exit           — Quit the agent
/help           — Show this help

Tips:
  • Ask the agent to build crypto projects and it will use all tools autonomously
  • The agent writes files, runs shell commands, fetches APIs — it actually BUILDS
  • Memory persists across sessions — tell it to remember things
  • Use /verbose to see the agent's reasoning process
"""
                if RICH:
                    console.print(Markdown(help_text))
                else:
                    print(help_text)
            else:
                if RICH:
                    console.print(f"[red]Unknown command: {cmd}. Type /help for commands.[/red]")
                else:
                    print(f"Unknown command: {cmd}")
            continue

        # ── Run agent on the prompt ──────────────────────
        run_agent(agent, prompt_text, verbose=verbose)


def main():
    parser = argparse.ArgumentParser(
        description="JDL Trade — Advanced Termux Coding Agent for Crypto Systems"
    )
    parser.add_argument("prompt", nargs="?", help="Single-shot prompt (skips REPL)")
    parser.add_argument("--resume", metavar="SESSION_ID", help="Resume a saved session")
    parser.add_argument("--sessions", action="store_true", help="List saved sessions")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show thinking output")
    parser.add_argument("--effort", choices=["low", "medium", "high", "max"],
                        help="Agent effort level (overrides config)")
    args = parser.parse_args()

    # Override effort if specified
    if args.effort:
        from agent.config import Config
        Config.EFFORT = args.effort

    # Ensure API key is available
    try:
        from agent.config import Config
        Config.validate()
    except ValueError as e:
        print(f"Configuration error: {e}")
        sys.exit(1)

    from agent.core import JDLAgent

    # List sessions and exit
    if args.sessions:
        sessions = JDLAgent.list_sessions()
        if not sessions:
            print("No saved sessions found.")
        for s in sessions:
            print(f"  {s['id']}  turns={s['turns']}  messages={s['messages']}")
        return

    # Initialize or resume agent
    if args.resume:
        try:
            agent = JDLAgent.load_session(args.resume)
            if RICH:
                console.print(f"[green]Resumed session: {args.resume}[/green]")
            else:
                print(f"Resumed: {args.resume}")
        except FileNotFoundError:
            print(f"Session not found: {args.resume}")
            sys.exit(1)
    else:
        agent = JDLAgent()

    # Single-shot mode
    if args.prompt:
        print_banner()
        run_agent(agent, args.prompt, verbose=args.verbose)
        return

    # Interactive REPL
    interactive_repl(agent, verbose=args.verbose)


if __name__ == "__main__":
    main()
