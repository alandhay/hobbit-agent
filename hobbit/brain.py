"""The model calls: one decision per move, and one reflection per attempt.

Locally the calls go through the Claude Agent SDK, which drives the installed Claude
Code with its login (no API key). The agent gets no tools at all: no files, no shell,
no web, and none of the account's connectors. Its only output is a JSON decision,
which the harness carries out.
"""

from __future__ import annotations

import asyncio
import base64
import time
from typing import Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ThinkingBlock,
    query,
)

from .config import Config

_ACTION = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["type", "key", "wait"]},
        "text": {"type": "string"},
    },
    "required": ["kind", "text"],
    "additionalProperties": False,
}

DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "observation": {"type": "string"},
        "screen_text": {"type": "array", "items": {"type": "string"}},
        "new_information": {"type": "string"},
        "previous_verdict": {"type": "string", "enum": ["met", "partly", "not_met", "n/a"]},
        "previous_note": {"type": "string"},
        "interpretation": {"type": "string"},
        "place": {"type": "string"},
        "options": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "action": _ACTION,
                    "why": {"type": "string"},
                    "expected": {"type": "string"},
                    "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                },
                "required": ["action", "why", "expected", "risk"],
                "additionalProperties": False,
            },
        },
        "chosen": {"type": "integer"},
        "action": _ACTION,
        "expected": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "mode": {"type": "string", "enum": ["explore", "exploit", "test", "recover"]},
        "confidence": {"type": "number"},
        "game_state": {"type": "string", "enum": ["title", "playing", "game_over", "won", "unknown"]},
        "notes": {"type": "string"},
        "recalled": {"type": "string"},
        "characters_seen": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "where": {"type": "string"},
                    "doing": {"type": "string"},
                },
                "required": ["name", "where", "doing"],
                "additionalProperties": False,
            },
        },
        "objects_seen": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "where": {"type": "string"},
                    "state": {"type": "string"},
                },
                "required": ["name", "where", "state"],
                "additionalProperties": False,
            },
        },
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "about": {"type": "string", "enum": ["place", "character", "object", "gameplay"]},
                    "name": {"type": "string"},
                    "fact": {"type": "string"},
                },
                "required": ["about", "name", "fact"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["observation", "screen_text", "new_information", "previous_verdict", "previous_note", "interpretation", "place", "options",
                 "chosen", "action", "expected", "evidence", "mode", "confidence", "game_state", "notes",
                 "recalled", "characters_seen", "objects_seen", "facts"],
    "additionalProperties": False,
}

_LESSON = {
    "type": "object",
    "properties": {
        "id": {"type": "string"},
        "text": {"type": "string"},
        "kind": {"type": "string", "enum": ["works", "fails", "unsure"]},
        "confidence": {"type": "number"},
        "support": {"type": "integer"},
        "contradict": {"type": "integer"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": ["active", "retired"]},
    },
    "required": ["id", "text", "kind", "confidence", "support", "contradict", "evidence", "status"],
    "additionalProperties": False,
}

REFLECT_SCHEMA = {
    "type": "object",
    "properties": {
        "lessons": {"type": "array", "items": _LESSON},
        "experiments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "why": {"type": "string"}},
                "required": ["text", "why"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["lessons", "experiments", "summary"],
    "additionalProperties": False,
}


REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "assessment": {"type": "string"},
        "tried": {"type": "array", "items": {"type": "string"}},
        "untried": {"type": "array", "items": {"type": "string"}},
        "verdict": {"type": "string", "enum": ["continue", "resign"]},
        "plan": {"type": "string"},
    },
    "required": ["assessment", "tried", "untried", "verdict", "plan"],
    "additionalProperties": False,
}


class BrainError(Exception):
    pass


class Brain:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.system = (cfg.prompts / "system.md").read_text(encoding="utf-8")
        self.reflect_prompt = (cfg.prompts / "reflect.md").read_text(encoding="utf-8")
        self.review_prompt = (cfg.prompts / "review.md").read_text(encoding="utf-8")
        self._cwd = cfg.data / "agent-cwd"         # an empty folder: nothing for Claude Code to pick up
        self._cwd.mkdir(parents=True, exist_ok=True)

    def _options(self, system: str, schema: dict, effort: str, model: str) -> ClaudeAgentOptions:
        return ClaudeAgentOptions(
            model=model,
            system_prompt=system,
            tools=[],
            allowed_tools=[],
            mcp_servers={},
            strict_mcp_config=True,             # none of the account's connectors
            setting_sources=[],                 # no CLAUDE.md, settings or memory
            extra_args={"disable-slash-commands": None},
            cwd=str(self._cwd),
            max_turns=3,
            effort=effort,
            thinking={"type": "adaptive", "display": "summarized"},
            output_format={"type": "json_schema", "schema": schema},
            include_partial_messages=True,
        )

    def decide(self, context: str, png: bytes, on_stream: Callable[[str, str], None] | None = None,
               model: str | None = None) -> tuple[dict, dict]:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(png).decode()}},
            {"type": "text", "text": context},
        ]
        options = self._options(self.system, DECISION_SCHEMA, self.cfg.effort, model or self.cfg.model)
        decision, record = asyncio.run(self._run(content, options, on_stream))
        if not 0 <= decision.get("chosen", -1) < len(decision.get("options", [])):
            decision["chosen"] = 0
        return decision, record

    def reflect(self, material: str) -> tuple[dict, dict]:
        options = self._options(self.reflect_prompt, REFLECT_SCHEMA, "medium", self.cfg.model)
        return asyncio.run(self._run([{"type": "text", "text": material}], options, None))

    def review(self, material: str, png: bytes) -> tuple[dict, dict]:
        """Step back from a run of moves that got nowhere: keep trying (with what plan), or resign?"""
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                         "data": base64.b64encode(png).decode()}},
            {"type": "text", "text": material},
        ]
        options = self._options(self.review_prompt, REVIEW_SCHEMA, "medium", self.cfg.model)
        return asyncio.run(self._run(content, options, None))

    async def _run(self, content: list[dict], options: ClaudeAgentOptions,
                   on_stream: Callable[[str, str], None] | None) -> tuple[dict, dict]:
        async def messages():
            yield {"type": "user", "message": {"role": "user", "content": content}, "parent_tool_use_id": None}

        started = time.time()
        thinking: list[str] = []
        text: list[str] = []
        result: ResultMessage | None = None
        async for message in query(prompt=messages(), options=options):
            if isinstance(message, StreamEvent):
                delta = message.event.get("delta", {}) if isinstance(message.event, dict) else {}
                if on_stream and delta.get("type") == "thinking_delta":
                    on_stream("thinking", delta.get("thinking", ""))
                elif on_stream and delta.get("type") == "input_json_delta":
                    on_stream("draft", delta.get("partial_json", ""))     # the decision as it is written
            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, ThinkingBlock):
                        thinking.append(block.thinking)
                    elif isinstance(block, TextBlock):
                        text.append(block.text)
            elif isinstance(message, ResultMessage):
                result = message
        if result is None or result.is_error or result.structured_output is None:
            detail = (result.errors if result else None) or "".join(text)[-500:] or "no result"
            raise BrainError(f"the model call failed: {detail}")
        record = {
            "seconds": round(time.time() - started, 1),
            "cost_usd": result.total_cost_usd or 0.0,
            "usage": result.usage,
            "thinking": "\n".join(thinking),
            "model": options.model,
        }
        return result.structured_output, record
