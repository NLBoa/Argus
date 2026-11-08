"""Goals ("get good at X", D079): week progress, on-plan streaks, the topic review ladder, and
trends. Pure and deterministic. The rule numbers come from `goals.rules` in config.yaml."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GoalRules:
    """The tunable numbers behind the goal rules (D079). Defaults are the student's first draft."""

    reteach_below: int = 60  # latest score below this: re-teach with a different method
    practice_below: int = 80  # below this: more practice; at or above: the review ladder
    review_ladder_days: tuple[int, ...] = (7, 14, 28)
    on_plan_share: float = 0.8  # a week is on plan when done >= share x target
    on_plan_weeks: int = 2  # finished on-plan weeks in a row before time may grow
