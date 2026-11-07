"""D04: immutable decision evidence, not a physical execution acknowledgement.

Pure adapter for existing PriorityDecision, independent of the installed
dispatcher. Logging and replay tests may serialize it consistently; no
Modbus/EVCC, files, services, or status changes occur in this module.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import math
import re
from typing import Literal

from energy_priority_policy import PriorityDecision


Quality = Literal["fresh", "stale", "unknown"]
SAFE_TAG = re.compile(r"^[a-zA-Z][a-zA-Z0-9_.:-]{0,79}$")


@dataclass(frozen=True)
class DispatchDecision:
    action: str
    power_w: int
    origin: str
    winner: str
    reason: str
    data_quality: Quality
    guard_results: tuple[str, ...]
    requires_register_readback: bool
    expected_soc_kwh: float | None = None
    solar_skip: bool = False
    physical_confirmation: Literal["unknown"] = "unknown"

    def __post_init__(self) -> None:
        if not all(isinstance(value, str) and SAFE_TAG.fullmatch(value)
                   for value in (self.action, self.origin, self.winner)):
            raise ValueError("invalid decision action or provenance tag")
        if type(self.power_w) is not int or not 0 <= self.power_w <= 100_000:
            raise ValueError("power_w must be a bounded integer")
        if self.data_quality not in ("fresh", "stale", "unknown"):
            raise ValueError("invalid data quality")
        if type(self.requires_register_readback) is not bool or type(self.solar_skip) is not bool:
            raise ValueError("readback and solar_skip must be booleans")
        if (self.expected_soc_kwh is not None and
                (type(self.expected_soc_kwh) not in (float, int)
                 or not math.isfinite(self.expected_soc_kwh)
                 or self.expected_soc_kwh < 0)):
            raise ValueError("invalid expected SoC")
        if not isinstance(self.guard_results, tuple) or any(
                not isinstance(item, str) or not SAFE_TAG.fullmatch(item)
                for item in self.guard_results):
            raise ValueError("guard identifiers must be immutable bounded tags")
        if not isinstance(self.reason, str) or len(self.reason) > 300:
            raise ValueError("invalid policy reason")
        if self.physical_confirmation != "unknown":
            raise ValueError("physical confirmation cannot be inferred from a decision")

    @classmethod
    def from_priority(
        cls, proposal: PriorityDecision, *, origin: str, data_quality: Quality,
        guard_results: tuple[str, ...], requires_register_readback: bool,
        expected_soc_kwh: float | None = None, solar_skip: bool = False,
    ) -> DispatchDecision:
        """Lossless action/watts/winner/reason compatibility with pure arbitration."""
        return cls(
            action=proposal.action, power_w=proposal.power_w, origin=origin,
            winner=proposal.winner, reason=proposal.reason,
            data_quality=data_quality, guard_results=guard_results,
            requires_register_readback=requires_register_readback,
            expected_soc_kwh=expected_soc_kwh, solar_skip=solar_skip,
        )

    def log_fields(self, *, observed_at: datetime) -> dict:
        """Immutable, source-only event payload shared by tests/future logger.

        This is *not* a control_actions.applied or independently verified
        physical event. Never reinterpret a quality flag as a safety GO.
        """
        if observed_at.tzinfo is None or observed_at.utcoffset() is None:
            raise ValueError("log timestamp must include timezone")
        return {
            "schema": "solinteg-dispatch-decision-v1",
            "timestamp": observed_at.isoformat(),
            "action": self.action, "power_w": self.power_w,
            "origin": self.origin, "policy_winner": self.winner,
            "reason": self.reason, "data_quality": self.data_quality,
            "guard_results": list(self.guard_results),
            "requires_register_readback": self.requires_register_readback,
            "expected_soc_kwh": self.expected_soc_kwh, "solar_skip": self.solar_skip,
            "decision_state": "proposed_unverified",
            "physical_confirmation": "unknown",
        }
