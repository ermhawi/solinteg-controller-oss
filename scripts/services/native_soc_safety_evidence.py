"""S09: documentary EndSOC preflight; never authorizes physical Modbus writes.

Economic reserve (planner), software discharge floor and the native inverter
cut-off are separate limits. Evidence may only qualify an *operator review*;
this function cannot grant a production GO or certify hardware behaviour.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class NativeSocEvidence:
    economic_reserve_pct: float
    software_floor_pct: float
    native_protection_enabled: bool | None = None
    native_end_soc_pct: float | None = None
    firmware_identified: bool = False
    register_scale_vendor_confirmed: bool = False
    vendor_ems_discharge_priority_confirmed: bool = False
    vendor_stale_setpoint_cutoff_confirmed: bool = False
    bms_response_verified: bool = False


def documentary_gate(evidence: NativeSocEvidence) -> dict:
    """Fail closed, without using historical raw 52503=100 as a percentage."""
    reasons: list[str] = []
    for field in ("economic_reserve_pct", "software_floor_pct"):
        pct = getattr(evidence, field)
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not math.isfinite(pct) or not 0 < pct <= 100:
            reasons.append("invalid-" + field)
    if evidence.native_protection_enabled is not True:
        reasons.append("native-protection-unverified")
    native = evidence.native_end_soc_pct
    if native is None or isinstance(native, bool) or not isinstance(native, (int, float)) or not math.isfinite(native) or not 0 < native <= 100:
        reasons.append("native-end-soc-unverified")
    elif (isinstance(evidence.economic_reserve_pct, (float, int))
          and math.isfinite(evidence.economic_reserve_pct)
          and native < evidence.economic_reserve_pct):
        reasons.append("native-below-economic-reserve")
    for field in (
        "firmware_identified", "register_scale_vendor_confirmed",
        "vendor_ems_discharge_priority_confirmed",
        "vendor_stale_setpoint_cutoff_confirmed", "bms_response_verified",
    ):
        if getattr(evidence, field) is not True:
            reasons.append("missing-" + field)
    return {
        "status": "documentary-review-eligible" if not reasons else "NO-GO",
        "reasons": reasons,
        "physical_go": False,
        "physical_state_verified": False,
        "operator_approval_required": True,
    }
