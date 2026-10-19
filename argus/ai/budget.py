"""Token prices and monthly spend. Prices are per million tokens (input, output, cache read, cache write)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from argus.clock import local_datetime, to_local
from argus.store.db import iso

# USD per 1M tokens. Update when Anthropic's prices change (docs: platform.claude.com/pricing).
PRICES: dict[str, tuple[float, float, float, float]] = {
    "claude-haiku-5-5": (0.10, 0.50, 0.01, 0.125),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20, 2.50),
    "claude-opus-5-5": (4.00, 20.00, 0.20, 5.00),
}
UNKNOWN_MODEL_PRICE = (10.00, 50.00, 1.00, 12.50)  # conservative: never under-count spend


def cost_usd(
    model: str, input_tokens: int, output_tokens: int, cache_read: int, cache_write: int
) -> float:
    p_in, p_out, p_read, p_write = PRICES.get(model, UNKNOWN_MODEL_PRICE)
    return (
        input_tokens * p_in + output_tokens * p_out + cache_read * p_read + cache_write * p_write
    ) / 1e6


def month_bounds(now: datetime) -> tuple[datetime, datetime]:
    """Start and end of the current local (America/New_York) month, as UTC instants."""
    local = to_local(now).date()
    start = local_datetime(local.replace(day=1), 0)
    nxt = (local.replace(day=28) + timedelta(days=4)).replace(day=1)
    return start, local_datetime(nxt, 0)


def month_key(now: datetime) -> str:
    return f"{to_local(now):%Y-%m}"


def spent_this_month(conn: sqlite3.Connection, now: datetime) -> float:
    start, end = month_bounds(now)
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) FROM ai_calls WHERE created_at >= ? AND created_at < ?",
        (iso(start), iso(end)),
    ).fetchone()
    return float(row[0])


def budget_hit_this_month(conn: sqlite3.Connection, now: datetime) -> bool:
    start, end = month_bounds(now)
    row = conn.execute(
        "SELECT 1 FROM ai_calls WHERE status = 'budget' AND created_at >= ? AND created_at < ? LIMIT 1",
        (iso(start), iso(end)),
    ).fetchone()
    return row is not None
