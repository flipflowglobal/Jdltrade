"""
Core agent loop for JDL Trade Coding Agent.
Uses Claude Opus 4.6 with adaptive thinking, streaming, and tool use.
"""

import json
import time
import sys
from pathlib import Path
from typing import Generator

import anthropic

from .config import Config
from .tools import ToolExecutor, TOOL_DEFINITIONS
from .prompts import SYSTEM_PROMPT, CRYPTO_CONTEXT


class JDLAgent:
    """
    Autonomous coding agent powered by Claude Opus 4.6.
    Features: adaptive thinking, streaming output, full tool suite,
    conversation memory, session persistence.
    """

    def __init__(self, display_callback=None):
        Config.validate()
        Config.ensure_dirs()

        self.client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
        self.executor = ToolExecutor()
        self.messages: list[dict] = []
        self.session_id = f"session_{int(time.time())}"
        self.turn_count = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0

        # Optional display callback for UI integration
        # callback(event_type, data) where event_type is:
        # 'thinking', 'text', 'tool_call', 'tool_result', 'error', 'usage'
        self.display = display_callback

    def _emit(self, event_type: str, data: str | dict):
        if self.display:
            self.display(event_type, data)

    def query(self, user_message: str) -> Generator[tuple[str, str], None, None]:
        """
        Send a message and yield (event_type, content) tuples as they stream in.
        Handles the full agentic loop: streaming → tool calls → results → continue.

        event_type values: 'thinking', 'text', 'tool_call', 'tool_result',
                           'usage', 'error', 'done'
        """
        self.turn_count += 1
        self.messages.append({"role": "user", "content": user_message})

        system = SYSTEM_PROMPT + "\n\n" + CRYPTO_CONTEXT

        while True:
            # Collect full response content for history
            response_content = []
            current_block_type = None
            current_block_id = None
            current_block_name = None
            current_text = ""
            current_thinking = ""
            current_tool_input_str = ""

            try:
                with self.client.messages.stream(
                    model=Config.MODEL,
                    max_tokens=Config.MAX_TOKENS,
                    thinking={"type": "adaptive"},
                    output_config={"effort": Config.EFFORT},
                    system=system,
                    tools=TOOL_DEFINITIONS,
                    messages=self.messages,
                ) as stream:

                    for event in stream:
                        etype = event.type

                        # ── Block start ──────────────────────────
                        if etype == "content_block_start":
                            cb = event.content_block
                            current_block_type = cb.type
                            current_block_id = getattr(cb, "id", None)
                            current_block_name = getattr(cb, "name", None)
                            current_text = ""
                            current_thinking = ""
                            current_tool_input_str = ""

                            if cb.type == "tool_use":
                                yield ("tool_call_start", {"name": cb.name, "id": cb.id})

                        # ── Block delta ──────────────────────────
                        elif etype == "content_block_delta":
                            delta = event.delta

                            if delta.type == "thinking_delta":
                                current_thinking += delta.thinking
                                yield ("thinking", delta.thinking)

                            elif delta.type == "text_delta":
                                current_text += delta.text
                                yield ("text", delta.text)

                            elif delta.type == "input_json_delta":
                                current_tool_input_str += delta.partial_json
                                yield ("tool_input", delta.partial_json)

                        # ── Block stop ───────────────────────────
                        elif etype == "content_block_stop":
                            if current_block_type == "thinking" and current_thinking:
                                response_content.append(
                                    {"type": "thinking", "thinking": current_thinking,
                                     "signature": ""}
                                )
                            elif current_block_type == "text" and current_text:
                                response_content.append(
                                    {"type": "text", "text": current_text}
                                )
                            elif current_block_type == "tool_use":
                                try:
                                    tool_input = json.loads(current_tool_input_str or "{}")
                                except json.JSONDecodeError:
                                    tool_input = {"raw": current_tool_input_str}
                                response_content.append({
                                    "type": "tool_use",
                                    "id": current_block_id,
                                    "name": current_block_name,
                                    "input": tool_input,
                                })

                        # ── Message delta (stop reason / usage) ──
                        elif etype == "message_delta":
                            if hasattr(event, "usage") and event.usage:
                                self.total_output_tokens += event.usage.output_tokens or 0

                    # Get the final message for accurate usage and stop reason
                    final = stream.get_final_message()
                    stop_reason = final.stop_reason

                    if final.usage:
                        self.total_input_tokens += final.usage.input_tokens or 0
                        cached = final.usage.cache_read_input_tokens or 0
                        yield ("usage", {
                            "input": final.usage.input_tokens,
                            "output": final.usage.output_tokens,
                            "cached": cached,
                            "total_in": self.total_input_tokens,
                            "total_out": self.total_output_tokens,
                        })

            except anthropic.RateLimitError as e:
                wait = int(getattr(e.response, "headers", {}).get("retry-after", "60"))
                yield ("error", f"Rate limited. Waiting {wait}s...")
                time.sleep(wait)
                continue
            except anthropic.APIStatusError as e:
                yield ("error", f"API error {e.status_code}: {e.message}")
                return
            except Exception as e:
                yield ("error", f"Unexpected error: {e}")
                return

            # Append assistant response to conversation history
            self.messages.append({"role": "assistant", "content": response_content})

            # ── Check if we need to execute tools ────────────
            if stop_reason != "tool_use":
                yield ("done", stop_reason or "end_turn")
                self._save_session()
                return

            # ── Execute all tool calls ────────────────────────
            tool_results = []
            for block in response_content:
                if block["type"] != "tool_use":
                    continue

                tool_name = block["name"]
                tool_input = block["input"]
                tool_id = block["id"]

                yield ("tool_call", {"name": tool_name, "input": tool_input, "id": tool_id})

                result = self.executor.execute(tool_name, tool_input)
                yield ("tool_result", {"name": tool_name, "result": result, "id": tool_id})

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_id,
                    "content": result,
                })

            # Feed tool results back and continue the loop
            self.messages.append({"role": "user", "content": tool_results})
            # Continue the while loop to get Claude's next response

    def reset(self):
        """Clear conversation history."""
        self.messages = []
        self.session_id = f"session_{int(time.time())}"
        self.turn_count = 0

    def _save_session(self):
        """Persist conversation to disk."""
        path = Config.SESSION_DIR / f"{self.session_id}.json"
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({
                    "session_id": self.session_id,
                    "turns": self.turn_count,
                    "total_input_tokens": self.total_input_tokens,
                    "total_output_tokens": self.total_output_tokens,
                    "messages": self.messages,
                }, f, indent=2, default=str)
        except Exception:
            pass  # Session save is best-effort

    @classmethod
    def load_session(cls, session_id: str) -> "JDLAgent":
        """Load a previous session."""
        path = Config.SESSION_DIR / f"{session_id}.json"
        if not path.exists():
            raise FileNotFoundError(f"Session not found: {session_id}")

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)

        agent = cls()
        agent.session_id = data["session_id"]
        agent.messages = data["messages"]
        agent.turn_count = data["turns"]
        agent.total_input_tokens = data.get("total_input_tokens", 0)
        agent.total_output_tokens = data.get("total_output_tokens", 0)
        return agent

    @classmethod
    def list_sessions(cls) -> list[dict]:
        """List all saved sessions."""
        sessions = []
        for path in sorted(Config.SESSION_DIR.glob("*.json"), reverse=True):
            try:
                with open(path) as f:
                    data = json.load(f)
                sessions.append({
                    "id": data.get("session_id", path.stem),
                    "turns": data.get("turns", 0),
                    "messages": len(data.get("messages", [])),
                })
            except Exception:
                continue
        return sessions
