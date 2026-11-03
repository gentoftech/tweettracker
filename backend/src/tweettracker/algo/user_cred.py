"""Exact port of UserCredV2.scala (user-cred-v2, launched 2026-04-27)."""

from __future__ import annotations

from dataclasses import dataclass
import math

SCORE_SLOPE: float = 7.07
SCORE_INTERCEPT: float = 165.2


@dataclass(frozen=True, slots=True)
class UserCredV2:
    """Exact port of com.twitter.health.platform_manipulation.user_cred_v2.UserCredV2."""

    id: int
    mass: float
    score: float

    @classmethod
    def from_mass(cls, user_id: int, mass: float) -> UserCredV2:
        if mass <= 0.0:
            raw_score = 0.0
        else:
            raw_score = SCORE_INTERCEPT + SCORE_SLOPE * math.log(mass)
        score = min(max(raw_score, 0.0), 100.0)
        return cls(id=user_id, mass=mass, score=score)
