"""Transient answer previews. Only the completed run is authoritative."""

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextvars import ContextVar
from dataclasses import dataclass, field

from pydantic_core import from_json


def public_steps(messages: list[dict], calls: list[dict], pending: str = "running") -> list[dict]:
    """Extract user-facing commentary and tool states, never private reasoning or arguments."""
    results = {call["call_id"]: call for call in calls}
    steps = []
    for message in messages:
        if message.get("role") != "assistant" or not message.get("tool_calls"):
            continue
        if message.get("content"):
            steps.append({"type": "commentary", "text": message["content"]})
        for call in message["tool_calls"]:
            result = results.get(call["id"])
            steps.append(
                {
                    "type": "tool",
                    "call_id": call["id"],
                    "name": call["function"]["name"],
                    "status": ("completed" if result["success"] else "failed")
                    if result
                    else pending,
                }
            )
    return steps


@dataclass
class LiveAnswer:
    text: str = ""
    steps: list[dict] = field(default_factory=list)
    version: int = 0
    finished: bool = False
    changed: asyncio.Event = field(default_factory=asyncio.Event)

    def set_steps(self, steps: list[dict]) -> None:
        if self.steps != steps:
            self.steps = steps
            self.wake()

    def set_text(self, text: str) -> None:
        if self.text != text:
            self.text = text
            self.wake()

    def wake(self) -> None:
        self.version += 1
        self.changed.set()
        self.changed = asyncio.Event()

    def close(self) -> None:
        self.finished = True
        self.wake()

    async def events(self) -> AsyncIterator[str]:
        version = -1
        previous = ""
        previous_steps = None
        while not self.finished:
            event = self.changed
            if version != self.version:
                version = self.version
                # A reconnect starts with an empty baseline; later frames carry only deltas.
                append = self.text.startswith(previous)
                body = {
                    "append": append,
                    "text": self.text[len(previous) :] if append else self.text,
                }
                if previous_steps != self.steps:
                    body["steps"] = self.steps
                    previous_steps = self.steps
                previous = self.text
                yield f"event: draft\ndata: {json.dumps(body)}\n\n"
            try:
                await asyncio.wait_for(event.wait(), 12)
            except TimeoutError:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.035)
        yield "event: settled\ndata: {}\n\n"


current_answer: ContextVar[LiveAnswer | None] = ContextVar("live_answer", default=None)


class StructuredPreview:
    """Publish only the top-level JSON content string; never tool arguments or reasoning."""

    def __init__(self) -> None:
        self.parts: list[str] = []
        self.last = 0.0

    def feed(self, delta: str) -> None:
        answer = current_answer.get()
        if answer is None:
            return
        self.parts.append(delta)
        if time.monotonic() - self.last < 0.04:
            return
        self.last = time.monotonic()
        try:
            value = from_json("".join(self.parts), allow_partial="trailing-strings")
        except ValueError:
            return
        if isinstance(value, dict) and isinstance(value.get("content"), str):
            answer.set_text(value["content"])
