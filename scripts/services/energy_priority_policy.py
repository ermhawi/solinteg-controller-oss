"""Final arbitration for the economic reserve versus physical solar capture.

Pure policy: no Modbus, service, or EVCC writes. The caller must execute the
existing independent SoC recovery and EV/phase safety guards before this step.
Only a verified, non-charging EV state permits entering General at low SoC.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Initial values for the offline replay; measure before enabling in production.
SOLAR_ENTER_W = 300
SOLAR_EXIT_W = -200
SOLAR_SAMPLE_MAX_AGE_S = 75
SOLAR_SAMPLE_MIN_GAP_S = 15
SOLAR_SAMPLE_MAX_GAP_S = 55
SOLAR_MIN_DWELL_S = 90
SOLAR_DRAIN_BUDGET_WH = 30
SOLAR_URGENT_DISCHARGE_W = 1500


@dataclass(frozen=True)
class SolarState:
    capturing: bool = False
    entered_at: datetime | None = None
    last_sample_at: datetime | None = None
    discharged_wh: float = 0.0
    blocked_until: datetime | None = None


@dataclass(frozen=True)
class PriorityDecision:
    action: str
    power_w: int
    state: SolarState
    winner: str
    reason: str
    solar_surplus_w: int | None = None
    sample_count: int = 0


def _valid_samples(readings: list[dict], now: datetime) -> list[tuple]:
    """Deduplicated original inverter samples, with power balance checked."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    now = now.astimezone(timezone.utc)
    checked = []
    seen = set()
    for row in readings:
        try:
            stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                continue
            stamp = stamp.astimezone(timezone.utc)
            if (stamp in seen or stamp < now - timedelta(seconds=180)
                    or stamp > now + timedelta(seconds=5)):
                continue
            if row["work_mode_raw"] not in (0x101, 0x303):
                continue
            pv, house, grid, battery = (
                float(row[k]) for k in ("pv_w", "house_load_w", "grid_w", "battery_w")
            )
            if (not all(math.isfinite(x) for x in (pv, house, grid, battery))
                    or not 0 <= pv <= 6500 or not 0 <= house <= 18000
                    or not -12000 <= grid <= 12000
                    or not -12000 <= battery <= 12000
                    or abs(pv + battery - house - grid) > max(400, pv * .2)):
                continue
            seen.add(stamp)
            checked.append((stamp, pv - house, grid, battery))
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
    return sorted(checked)


def _stable_solar(samples: list[tuple], now: datetime) -> bool:
    if len(samples) < 2:
        return False
    prev, last = samples[-2:]
    interval = (last[0] - prev[0]).total_seconds()
    age = (now - last[0]).total_seconds()
    return (0 <= age <= SOLAR_SAMPLE_MAX_AGE_S
            and SOLAR_SAMPLE_MIN_GAP_S <= interval <= SOLAR_SAMPLE_MAX_GAP_S
            and prev[1] >= SOLAR_ENTER_W and last[1] >= SOLAR_ENTER_W
            and (last[2] >= 100 or last[3] <= -200))


def resolve(action: str, power_w: int, *, now: datetime,
            readings: list[dict], state: SolarState, soc_pct: float | None,
            reserve_pct: float, save_w: int, recovery: bool,
            ev_charging: bool | None, solar_skip: bool = False) -> PriorityDecision:
    """Select one action after safety/EV proposals, before hard charge caps.

    Under the economic reserve, planned charging and physical recovery always
    win. Inactivity/save may switch to General only on stable solar, and the
    General latch survives transient household spikes. Unverified EV status
    forbids a NEW low-SoC General transition; EMS does not guarantee zero draw.
    """
    if action not in ("charge", "solar_charge", "idle", "discharge",
                      "freeze", "reserve_hold"):
        return PriorityDecision(action, power_w, SolarState(), "unsupported",
                                "Unrecognized action must be handled by the executor")
    if recovery:
        return PriorityDecision(action, power_w, SolarState(), "soc_recovery",
                                "Physical SoC recovery has absolute policy priority")
    if action in ("solar_charge", "freeze", "reserve_hold"):
        return PriorityDecision(action, power_w, SolarState(), "explicit_control",
                                "Independent explicit control is retained")
    if action == "charge" and not solar_skip:
        return PriorityDecision(action, power_w, SolarState(), "planned_charge",
                                "Planned or EV-priority charging has precedence")
    if save_w <= 0:
        return PriorityDecision(action, power_w, SolarState(), "save_disabled",
                                "Economic battery-save emulation is not configured")

    if soc_pct is None or not math.isfinite(soc_pct) or not 0 <= soc_pct <= 100:
        return PriorityDecision(action, power_w, SolarState(), "unverified_soc",
                                "No verified SoC; retain pre-existing decision")
    if soc_pct > reserve_pct:
        return PriorityDecision(action, power_w, SolarState(), "economic_plan",
                                "Above the economic reserve: preserve optimizer decision")

    samples = _valid_samples(readings, now)
    latest = samples[-1] if samples else None
    fresh = (latest is not None and
             0 <= (now.astimezone(timezone.utc) - latest[0]).total_seconds()
             <= SOLAR_SAMPLE_MAX_AGE_S)
    surplus = round(latest[1]) if fresh else None

    # Solar funding can veto a planned charge, but must not implicitly select
    # General when the battery is already below its economic reserve.
    if action == "charge" and solar_skip:
        action, power_w = "idle", 0

    blocked_until = (state.blocked_until if state.blocked_until is not None
                     and now < state.blocked_until else None)
    if ev_charging is not False or not fresh:
        winner = "ev_grid_priority" if ev_charging is True else (
            "unverified_ev" if ev_charging is None else "unverified_solar")
        return PriorityDecision("discharge", save_w,
                                SolarState(blocked_until=blocked_until), winner,
                                "Preserve economic reserve when EV or PV data is not verified",
                                surplus, len(samples))

    capturing = state.capturing and state.entered_at is not None
    entered_at = state.entered_at if capturing else None
    last_seen = state.last_sample_at if capturing else None
    drained_wh = state.discharged_wh if capturing else 0.0
    if capturing:
        previous = last_seen or entered_at
        for stamp, _surplus, _grid, battery in samples:
            if stamp <= previous:
                continue
            seconds = min(55, (stamp - previous).total_seconds())
            drained_wh += max(0.0, battery) * seconds / 3600
            previous = stamp
        last_seen = previous
        latest_urgent = latest[3] >= SOLAR_URGENT_DISCHARGE_W
        two_deficits = (len(samples) >= 2 and
                        all(row[1] <= SOLAR_EXIT_W for row in samples[-2:]))
        two_heavy_discharge = (len(samples) >= 2 and
                               all(row[3] > 600 for row in samples[-2:]))
        dwell = (now - entered_at).total_seconds()
        if latest_urgent or drained_wh >= SOLAR_DRAIN_BUDGET_WH:
            capturing = False
            blocked_until = now + timedelta(seconds=180)
        elif dwell >= SOLAR_MIN_DWELL_S and (two_deficits or two_heavy_discharge):
            capturing = False
            blocked_until = now + timedelta(seconds=90)

    if capturing:
        next_state = SolarState(True, entered_at, last_seen, drained_wh)
        return PriorityDecision("idle", 0, next_state, "solar_capture",
                                "Maintain General during transient PV/house load variation",
                                surplus, len(samples))

    if blocked_until is not None:
        return PriorityDecision("discharge", save_w,
                                SolarState(blocked_until=blocked_until),
                                "solar_cooldown",
                                "Recent General discharge/deficit requires a cooldown",
                                surplus, len(samples))

    if _stable_solar(samples, now):
        next_state = SolarState(True, now, latest[0], 0.0)
        return PriorityDecision("idle", 0, next_state, "solar_capture",
                                "Stable measured surplus has priority over EMS battery save",
                                surplus, len(samples))

    return PriorityDecision("discharge", save_w, SolarState(), "economic_reserve",
                            "Avoid General discharge until solar is stable",
                            surplus, len(samples))
