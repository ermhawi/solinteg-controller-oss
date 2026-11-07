"""S06 source-only supervisor authorization and conservative safety preflight.

No Modbus I/O here. This is an additional offline safety contract, not
an authorized or deployed independent recovery mechanism.
"""
from __future__ import annotations

import math


SAFETY_ENABLED_ENV = "SOLINTEG_BROKER_SAFETY_AUTO_ENABLED"
SUPERVISOR_UNIT = "solinteg-broker-safety-supervisor.service"
MIN_SAFE_AUTO_SOC_PCT = 20.0
GENERAL_MODE = 0x101
EMS_BATTCTRL_MODE = 0x303


def is_supervisor_enabled(environ) -> bool:
    """Require explicit opt-in; no truthy aliases, wildcards or defaults."""
    return environ.get(SAFETY_ENABLED_ENV) == "1"


def approve_supervisor_role(unit: str, environ) -> bool:
    return unit == SUPERVISOR_UNIT and is_supervisor_enabled(environ)


def validate_safety_auto_preflight(snapshot: dict, *, broker_owns_control: bool) -> str:
    """Require current complete register/CT/SoC proof, never infer BMS safety.

    Return `already_general` without any FC06 or `owned_ems` for the only
    mode that may proceed to the existing serial broker worker.
    """
    if not isinstance(snapshot, dict) or snapshot.get("status") != "valid":
        raise PermissionError("supervisor: fresh complete Modbus snapshot required")
    sample = snapshot.get("sample")
    if not isinstance(sample, dict) or sample.get("quality") != "valid":
        raise PermissionError("supervisor: invalid source sample")
    soc = sample.get("soc_pct")
    if (type(soc) not in (float, int) or not math.isfinite(soc)
            or soc < MIN_SAFE_AUTO_SOC_PCT or soc > 100):
        raise PermissionError("supervisor: native protection unverified at this SoC")
    mode = sample.get("work_mode_raw")
    if type(mode) is not int or mode not in (GENERAL_MODE, EMS_BATTCTRL_MODE):
        raise PermissionError("supervisor: unexpected or unknown mode")
    if mode == GENERAL_MODE:
        return "already_general"
    if broker_owns_control is not True:
        raise PermissionError("supervisor: existing EMS control ownership unverified")
    return "owned_ems"
