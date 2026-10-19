"""The only door to the Anthropic API.

`AIClient.structured()` returns a validated Pydantic object, or `None`, in which case the caller
uses its non-AI fallback. Nothing in Argus depends on an AI call succeeding.

Order of operations for one call:
1. Cache: an identical (model, prompt version, rendered input, schema) request returns the stored answer.
2. Budget: if this month's spend has reached the cap, no call is made (status `budget`). The
   reminder engine sends one system alert per month from that record.
3. Request: JSON-schema structured output, prompt-cached system block.
4. Validate with Pydantic. On failure, retry once; then give up (status `invalid`).
   Refusals and API errors give up immediately (the SDK already retried transient errors).
Every attempt is written to `ai_calls` with tokens and cost.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, ValidationError

from argus.ai.budget import cost_usd, spent_this_month
from argus.ai.prompts import RenderedPrompt
from argus.ai.schemas import strict_schema
from argus.clock import Clock
from argus.config import AIConfig
from argus.logging import get_logger, redactor
from argus.store.db import iso, transaction

log = get_logger(__name__)
# Models that accept server-side refusal fallbacks (`fallbacks: "default"`).
SERVER_FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-sonnet-5-5"})
SERVER_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class Tier(StrEnum):
    FAST = "fast"
    STANDARD = "standard"
    STRONG = "strong"


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0


@dataclass(frozen=True, slots=True)
class TransportResponse:
    text: str | None
    stop_reason: str
    usage: Usage
    model: str


class TransportError(Exception):
    """The API call failed (after the SDK's own retries)."""


class Transport(Protocol):
    async def create(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int,
        effort: str,
    ) -> TransportResponse: ...


class AnthropicTransport:
    """Real transport over the official SDK. Constructed only when an API key is configured."""

    def __init__(self, api_key: str, *, timeout: float = 120.0) -> None:
        import anthropic

        self._anthropic = anthropic
        self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout, max_retries=2)

    async def create(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int,
        effort: str,
    ) -> TransportResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}],
            "output_config": {
                "format": {"type": "json_schema", "schema": schema},
                "effort": effort,
            },
        }
        try:
            if model in SERVER_FALLBACK_MODELS:
                msg: Any = await self._client.beta.messages.create(
                    **kwargs, betas=[SERVER_FALLBACK_BETA], fallbacks="default"
                )
            else:
                msg = await self._client.messages.create(**kwargs)
        except self._anthropic.APIError as e:
            raise TransportError(f"{type(e).__name__}: {redactor(str(e))[:300]}") from e
        text = next((b.text for b in msg.content if getattr(b, "type", None) == "text"), None)
        u = msg.usage
        return TransportResponse(
            text=text,
            stop_reason=str(msg.stop_reason),
            usage=Usage(
                input_tokens=u.input_tokens or 0,
                output_tokens=u.output_tokens or 0,
                cache_read=u.cache_read_input_tokens or 0,
                cache_write=u.cache_creation_input_tokens or 0,
            ),
            model=str(msg.model),
        )


@dataclass(frozen=True, slots=True)
class AIResult[V: BaseModel]:
    value: V | None
    status: str  # ok | cache_hit | invalid | refused | error | budget | unavailable
    model: str
    prompt_id: str

    @property
    def ok(self) -> bool:
        return self.value is not None


class AIClient:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config: AIConfig,
        clock: Clock,
        transport: Transport | None,
    ) -> None:
        self.conn = conn
        self.config = config
        self.clock = clock
        self.transport = transport

    @property
    def available(self) -> bool:
        return self.transport is not None

    def model_for(self, tier: Tier) -> str:
        return str(getattr(self.config.models, tier.value))

    def _record(
        self,
        *,
        task: str,
        model: str,
        prompt_id: str,
        key: str,
        status: str,
        usage: Usage = Usage(),  # noqa: B008 (immutable)
        error: str | None = None,
    ) -> None:
        cost = cost_usd(
            model, usage.input_tokens, usage.output_tokens, usage.cache_read, usage.cache_write
        )
        with transaction(self.conn):
            self.conn.execute(
                """INSERT INTO ai_calls (task, model, prompt_version, input_hash, status, input_tokens,
                     output_tokens, cache_read, cache_write, cost_usd, error, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task,
                    model,
                    prompt_id,
                    key,
                    status,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.cache_read,
                    usage.cache_write,
                    cost,
                    error,
                    iso(self.clock.now()),
                ),
            )

    def _cached(self, key: str) -> str | None:
        row = self.conn.execute("SELECT response FROM ai_cache WHERE key = ?", (key,)).fetchone()
        return str(row[0]) if row else None

    def _store(self, key: str, task: str, payload: str) -> None:
        with transaction(self.conn):
            self.conn.execute(
                "INSERT OR REPLACE INTO ai_cache (key, task, response, created_at) VALUES (?, ?, ?, ?)",
                (key, task, payload, iso(self.clock.now())),
            )

    async def structured[T: BaseModel](
        self,
        *,
        task: str,
        tier: Tier,
        prompt: RenderedPrompt,
        schema: type[T],
        max_tokens: int = 4096,
        effort: str = "medium",
        use_cache: bool = True,
    ) -> AIResult[T]:
        model = self.model_for(tier)
        key = hashlib.sha256(
            json.dumps([model, prompt.id, prompt.system, prompt.user, schema.__name__]).encode()
        ).hexdigest()

        if use_cache and (hit := self._cached(key)) is not None:
            try:
                value = schema.model_validate_json(hit)
            except ValidationError:
                pass  # schema changed since: fall through and ask again
            else:
                self._record(
                    task=task, model=model, prompt_id=prompt.id, key=key, status="cache_hit"
                )
                return AIResult(value, "cache_hit", model, prompt.id)

        if self.transport is None:
            return AIResult(None, "unavailable", model, prompt.id)

        if spent_this_month(self.conn, self.clock.now()) >= self.config.monthly_budget_usd:
            self._record(task=task, model=model, prompt_id=prompt.id, key=key, status="budget")
            log.warning("ai budget reached; using fallback", extra={"task": task})
            return AIResult(None, "budget", model, prompt.id)

        json_schema = strict_schema(schema)
        last = "invalid"
        for attempt in range(2):
            try:
                resp = await self.transport.create(
                    model=model,
                    system=prompt.system,
                    user=prompt.user,
                    schema=json_schema,
                    max_tokens=max_tokens,
                    effort=effort,
                )
            except TransportError as e:
                self._record(
                    task=task,
                    model=model,
                    prompt_id=prompt.id,
                    key=key,
                    status="error",
                    error=str(e),
                )
                return AIResult(None, "error", model, prompt.id)
            if resp.stop_reason == "refusal":
                self._record(
                    task=task,
                    model=resp.model,
                    prompt_id=prompt.id,
                    key=key,
                    status="refused",
                    usage=resp.usage,
                )
                return AIResult(None, "refused", model, prompt.id)
            try:
                if resp.text is None or resp.stop_reason == "max_tokens":
                    raise ValueError(f"no complete JSON (stop_reason={resp.stop_reason})")
                value = schema.model_validate_json(resp.text)
            except (ValueError, ValidationError) as e:
                self._record(
                    task=task,
                    model=resp.model,
                    prompt_id=prompt.id,
                    key=key,
                    status="invalid",
                    usage=resp.usage,
                    error=f"attempt {attempt + 1}: {str(e)[:300]}",
                )
                last = "invalid"
                continue
            self._record(
                task=task,
                model=resp.model,
                prompt_id=prompt.id,
                key=key,
                status="ok",
                usage=resp.usage,
            )
            self._store(key, task, value.model_dump_json())
            return AIResult(value, "ok", model, prompt.id)
        return AIResult(None, last, model, prompt.id)


def build_transport(api_key: str | None) -> Transport | None:
    return AnthropicTransport(api_key) if api_key else None
