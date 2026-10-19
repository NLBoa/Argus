"""Test doubles for the AI boundary. No test outside tests/evals touches the network."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from argus.ai.client import TransportError, TransportResponse, Usage

FIXTURES = Path(__file__).parent / "fixtures" / "ai"
USAGE = Usage(input_tokens=1200, output_tokens=300, cache_read=0, cache_write=0)


def ok(payload: Any, model: str = "claude-haiku-5-5", usage: Usage = USAGE) -> TransportResponse:
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return TransportResponse(text=text, stop_reason="end_turn", usage=usage, model=model)


def refused(model: str = "claude-haiku-5-5") -> TransportResponse:
    return TransportResponse(text=None, stop_reason="refusal", usage=Usage(500, 5), model=model)


def recorded(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text())


@dataclass
class FakeTransport:
    """Replays queued responses (or a responder function) and records every request."""

    responses: list[TransportResponse | Exception] = field(default_factory=list)
    responder: Callable[[dict[str, Any]], TransportResponse] | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def create(self, **kwargs: Any) -> TransportResponse:
        self.calls.append(kwargs)
        if self.responder is not None:
            return self.responder(kwargs)
        if not self.responses:
            raise AssertionError("FakeTransport: no response queued")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def error(message: str = "APIConnectionError: boom") -> TransportError:
    return TransportError(message)
