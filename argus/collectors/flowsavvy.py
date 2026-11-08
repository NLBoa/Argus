"""FlowSavvy: goal sessions for the goals page (D079).

Sessions are FlowSavvy tasks titled "[Tag] what", where Tag is a goal's tag. Argus reads them
with FlowSavvy's public API (Pro plan, `Authorization: Bearer <key>`) and only ever sends GET
requests. The fetch itself lands with the collector proper; until then the source is a stub
that reports SKIPPED, so capabilities can already say whether it is set up.
"""

from __future__ import annotations

from argus.collectors.base import CollectContext, CollectResult, RunStatus
from argus.domain.models import Source


class FlowSavvyCollector:
    source = Source.FLOWSAVVY

    def enabled(self, ctx: CollectContext) -> bool:
        """Both halves: the key, and a `goals:` block to give sessions a place to show."""
        return ctx.config.goals is not None and ctx.secrets.flowsavvy_api_key is not None

    async def collect(self, ctx: CollectContext) -> CollectResult:
        return CollectResult(self.source, RunStatus.SKIPPED)
