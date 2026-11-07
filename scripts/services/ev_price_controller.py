#!/usr/bin/env python3
"""Single EVCC price executor; normally disabled without an accepted EV session.

The house optimizer remains independent. EV power is measured by EVCC Ict2
and never deducted from the physical CT fuse limits. There is one writer for
the EVCC register, serialized against the existing FC03 observer lock.
Missing/old session, ambiguous price, hardware state or preflight -> NO WRITE.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import ev_price_pause_owner as ownership
import ev_price_empirical_session as empirical
import evcc_cable_tracker as cable_tracker
from ha_ev_session import load_settings
import ev_volvo_smart_charging as smart
from ev_price_policy import decide_grid_price, read_ev_session, read_house_price_plan, PriceDecision
from evcc_ascii import (EvccAscii, RAW_PAUSE, EvccError, EvccUncertainWrite,
                        observed_evcc_mode)

EVCC = Path("/opt/solinteg/ev-shadow-runtime/var/evcc-readonly.json")
LIVE = Path("/opt/solinteg/live.json")
LOCK = Path("/opt/solinteg/ev-shadow-runtime/var/evcc-reader.lock")
MAX_SAMPLE_AGE_S = 65
MAX_PHASE_IMPORT_W = 3000
MAX_SITE_IMPORT_W = 6000
EV_RAW_6A = 100
EV_W_6A = 1380
OWNER = Path("/opt/solinteg/ev-shadow-runtime/var/ev-price-pause-owner.json")
OWNER_LOCK = Path("/opt/solinteg/ev-shadow-runtime/var/ev-price-pause-owner.lock")
SMART_CHARGING = Path("/opt/solinteg/ev-shadow-runtime/var/volvo-smart-charging.json")


@dataclass(frozen=True)
class EvPriceAction:
    action: str  # pause, resume, no_change or inhibited
    expected_raw: int | None
    target_raw: int | None
    reason: str


def choose_action(*, session: dict | None, price: PriceDecision,
                  evcc: dict | None, live: dict | None,
                  now: datetime, price_pause_owned: bool = False) -> EvPriceAction:
    """Pure command selection: requires verified session and valid snapshots."""
    def no(reason: str) -> EvPriceAction:
        return EvPriceAction("inhibited", None, None, reason)

    if session is None or now.tzinfo is None:
        return no("missing calibrated vehicle energy session")
    if not isinstance(evcc, dict) or not isinstance(live, dict):
        return no("missing EVCC or grid observation")
    if session.get("remaining_ac_kwh") is None:
        return no("missing verified EV energy requirement")
    if price.charge_mode != session.get("charge_mode"):
        return no("price mode and vehicle session disagree")
    try:
        stamp = datetime.fromisoformat(str(evcc["timestamp"]).replace("Z", "+00:00"))
        live_at = datetime.fromisoformat(str(live["timestamp"]).replace("Z", "+00:00"))
        if stamp.tzinfo is None or live_at.tzinfo is None:
            raise ValueError("naive timestamp")
        if not (0 <= (now - stamp).total_seconds() <= MAX_SAMPLE_AGE_S
                and 0 <= (now - live_at).total_seconds() <= MAX_SAMPLE_AGE_S):
            return no("stale EVCC or grid snapshot")
        if (evcc.get("source") != "waveshare_evcc_fc03_readonly"
                or evcc.get("read_ok") is not True
                or evcc.get("writes") != 0
                or evcc.get("physical_control_eligible") is not False
                or evcc.get("state") not in ("B2", "C2")):
            return no("EVCC state or independent FC03 source unverified")
        raw = evcc["configured_pwm_raw"]
        currents = evcc["channel_currents_a"]
        if observed_evcc_mode(
                evcc["state"], raw, evcc.get("pilot_pwm_raw"), currents) is None:
            return no("EVCC pilot, state or phase current inconsistent; never overwrite")
        if (live.get("grid_unbalanced_output_raw") != 0
                or any(type(live.get(k)) not in (int, float) or not math.isfinite(live[k])
                       for k in ("grid_w", "meter_l3_w"))):
            return no("missing valid phase import measurements")
        if session["remaining_ac_kwh"] <= 0:
            if raw == RAW_PAUSE:
                return EvPriceAction("no_change", raw, raw, "target already reached")
            if evcc["state"] != "C2" or currents[1] < 5.5:
                return no("vehicle already stopped; never take ownership of manual pause")
            return EvPriceAction("pause", raw, RAW_PAUSE, "verified 80 percent target reached")
        if price.buy_ore is None:
            return no("no confirmed current price decision")
        if price.grid_budget_a not in (0, 6):
            return no("unsupported EVCC pilot limit")
        if price.grid_budget_a == 0:
            if raw == RAW_PAUSE:
                return EvPriceAction("no_change", raw, raw, "already paused for expensive slot")
            if evcc["state"] != "C2" or currents[1] < 5.5:
                return no("vehicle already stopped; never take ownership of manual pause")
            return EvPriceAction("pause", raw, RAW_PAUSE, "confirmed expensive slot")
        if raw == EV_RAW_6A:
            return EvPriceAction("no_change", raw, raw,
                                 "already charging at permitted 6 A: " + price.reason)
        # A manual EVCC pause must not be undone by the price timer. Only
        # a persisted, independently verified pause previously made by this
        # controller may ever be resumed automatically. That persistent owner
        # protocol is required for every automated resume, including empiric mode.
        if price_pause_owned is not True:
            return no("pause owner unknown; manual pause has priority")
        # A conservative grid-import start preflight; the existing electrical
        # breaker is separate hardware protection. Never use CT to infer EV SoC.
        projected_l3 = max(0, -live["meter_l3_w"]) + EV_W_6A
        projected_site = max(0, -live["grid_w"]) + EV_W_6A
        if projected_l3 > MAX_PHASE_IMPORT_W or projected_site > MAX_SITE_IMPORT_W:
            return no("projected L3 or total grid import exceeds soft budget")
        if currents[1] > .5:
            return no("EVCC is reportedly paused but substantial current is flowing")
        return EvPriceAction("resume", raw, EV_RAW_6A, "confirmed cheap slot, 6 A only")
    except (KeyError, TypeError, ValueError, OverflowError):
        return no("invalid EVCC or grid snapshot")


def missing_session_reason(*, now: datetime, settings_path: Path,
                           cable_path: Path, ledger_path: Path) -> str:
    """Explain a verified missing mission, without treating the ledger as control authority."""
    try:
        cable = cable_tracker.read(cable_path)
        if (cable is not None and cable.get("status") == "disconnected"
                and cable.get("observed_state") in ("A1", "A2")
                and cable.get("candidate_status") is None):
            sampled = datetime.fromisoformat(cable["last_sample_at_utc"].replace("Z", "+00:00"))
            if sampled.tzinfo is not None and 0 <= (now - sampled).total_seconds() <= 90:
                return "Bilen är frånkopplad; inga laddkommandon skickas"
    except (OSError, ValueError, TypeError, KeyError, cable_tracker.CableObservationError):
        pass

    try:
        settings = load_settings(str(settings_path))
        ledger = empirical._ledger_read(ledger_path)
        if (ledger is not None and settings["charge_mode"] == "deadline"
                and ledger.get("charge_mode") == "deadline"
                and ledger.get("settings_revision") == settings.get("revision")):
            deadline = datetime.fromisoformat(ledger["departure_at_utc"].replace("Z", "+00:00"))
            if deadline.tzinfo is not None and 0 <= (now - deadline).total_seconds() <= 72 * 3600:
                return "Avresetiden har passerat; den tidigare laddsessionen har löpt ut"
    except (OSError, ValueError, TypeError, KeyError, empirical.EmpiricalSessionError):
        pass
    return "no calibrated fresh EV session"


def cable_matches_locked_preflight(cable: dict, latest: dict | None,
                                  evcc: dict) -> bool:
    """Pure final cable/CP check; caller holds both EVCC serialization locks."""
    if not isinstance(cable, dict) or not isinstance(latest, dict) or not isinstance(evcc, dict):
        return False
    for key in ("connection_epoch", "coverage_epoch", "tracking_id"):
        if key not in cable or latest.get(key) != cable[key]:
            return False
    return latest.get("observed_state") == evcc.get("state")


def intent_matches_locked_preflight(settings: dict, session: dict) -> bool:
    """Pure mission revision check; any malformed or changed intent inhibits."""
    if not isinstance(settings, dict) or not isinstance(session, dict):
        return False
    return all(
        skey in settings and tkey in session and settings[skey] == session[tkey]
        for skey, tkey in (
            ("revision", "settings_revision"),
            ("charge_mode", "charge_mode"),
            ("departure_local_time", "departure_local_time"),
            ("max_buy_ore_per_kwh", "max_buy_ore_per_kwh"),
        )
    )


def execute_locked_fc10(*, command: EvPriceAction, owner: dict | None,
                        owner_path: Path, session: dict, cable: dict,
                        now: datetime, host: str) -> dict:
    """Exactly one physical FC10, with durable ownership and readback.

    Called ONLY after price-owner and EVCC reader locks, revalidated cable
    and revision, and pure choose_action. No retry after unknown ACK.
    """
    if command.action == "pause":
        next_owner = ownership.new_owner(
            session=session, cable=cable, now=now, owner_id=str(uuid4()))
        ownership.write_owner(owner_path, next_owner)
    elif command.action == "resume":
        # If the FC10 outcome is uncertain or a process crashes,
        # 'releasing' is never eligible for another auto-resume.
        next_owner = {**owner, "state": "releasing"}
        ownership.write_owner(owner_path, next_owner)
    else:
        raise ValueError("only explicit pause or resume may execute FC10")
    client = EvccAscii(host, port=int(os.environ.get("EVCC_PORT", "4196")),
                       scheduled_control=True)
    after = client.set_limit_raw(
        command.target_raw, expected_raw=command.expected_raw, scheduled=True)
    if (after.configured_raw != command.target_raw
            or (command.action == "pause" and observed_evcc_mode(
                after.state, after.configured_raw,
                after.pilot_raw, after.currents_a) != "paused_connected")
            or (command.action == "resume"
                and after.pilot_raw != command.target_raw)):
        raise EvccUncertainWrite("EVCC readback mismatched")
    ownership.write_owner(
        owner_path, {**next_owner,
                     "verified_fc10_pause": True,
                     "state": "owned" if command.action == "pause" else "released"})
    return {"state": command.action, "writes": 1,
            "configured_raw": after.configured_raw, "reason": command.reason}


def run_once(*, now: datetime | None = None) -> dict:
    """One decision, one FC10 at most; durable owner changes precede writes."""
    now = now or datetime.now(timezone.utc)
    if (os.environ.get("EV_PRICE_CONTROL_ENABLED") != "1"
            or os.environ.get("EVCC_PRICE_CONTROL_ARMED") != "1"):
        return {"state": "disabled", "writes": 0, "reason": "price controller not armed"}

    settings_path = Path(os.environ.get("EV_SETTINGS_PATH",
        "/opt/solinteg/ev-shadow-runtime/var/settings.json"))
    session = read_ev_session(os.environ.get("EV_SESSION_PATH"), now=now)
    if session is None and os.environ.get("EV_PRICE_ACCEPT_EMPIRICAL_ENERGY") == "1":
        session = empirical.read_session(
            status_path=Path(os.environ.get("EV_STATUS_PATH",
                "/opt/solinteg/ev-shadow-runtime/var/status.json")),
            settings_path=settings_path,
            cable_path=Path(os.environ.get("EVCC_CABLE_TRACK_PATH",
                "/opt/solinteg/ev-shadow-runtime/var/evcc-cable-state.json")),
            ledger_path=Path(os.environ.get("EV_PRICE_EMPIRICAL_LEDGER_PATH",
                "/opt/solinteg/ev-shadow-runtime/var/ev-price-empirical-session.json")),
            now=now,
            expected_vin_sha256=os.environ.get("EV_VOLVO_TARGET_VIN_SHA256"),
        )
    price = (decide_grid_price(
        read_house_price_plan(os.environ.get("TELEMETRY_DB_PATH", "/opt/solinteg/telemetry.db")),
        now=now, session=session, max_current_a=6,
        forecast_risk_ore=float(os.environ.get("SOLINTEG_FORECAST_RISK_PREMIUM_ORE", "10")))
        if session is not None else PriceDecision(0, None, None, "no session"))
    owner_path = Path(os.environ.get("EV_PRICE_PAUSE_OWNER_PATH", str(OWNER)))
    lock_path = Path(os.environ.get("EV_PRICE_PAUSE_OWNER_LOCK", str(OWNER_LOCK)))
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        owner_lock = os.fdopen(fd, "a+b")
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077
                or info.st_uid != os.geteuid()):
            owner_lock.close()
            raise PermissionError("unsafe price owner lock")
    except (OSError, PermissionError):
        return {"state": "inhibited", "writes": 0, "reason": "pause owner lock unavailable"}
    with owner_lock:
        try:
            fcntl.flock(owner_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"state": "inhibited", "writes": 0, "reason": "another price controller owns lock"}
        try:
            try:
                owner = ownership.read_owner(owner_path)
            except ownership.OwnershipError:
                return {"state": "inhibited", "writes": 0, "reason": "unverifiable persistent pause owner"}

            def revoke(reason: str) -> None:
                if owner is not None and owner["state"] == "owned":
                    ownership.write_owner(owner_path, {**owner, "state": "revoked",
                                                       "reason": reason})

            if session is None:
                revoke("verified vehicle session lost")
                return {"state": "inhibited", "writes": 0,
                        "reason": missing_session_reason(
                            now=now, settings_path=settings_path,
                            cable_path=Path(os.environ.get("EVCC_CABLE_TRACK_PATH",
                                "/opt/solinteg/ev-shadow-runtime/var/evcc-cable-state.json")),
                            ledger_path=Path(os.environ.get("EV_PRICE_EMPIRICAL_LEDGER_PATH",
                                "/opt/solinteg/ev-shadow-runtime/var/ev-price-empirical-session.json")))}
            smart_state, smart_reason = smart.read_smart_charging(
                os.environ.get("EV_VOLVO_SMART_CHARGING_SNAPSHOT_PATH", str(SMART_CHARGING)),
                now=now, expected_vin_sha256=os.environ.get("EV_VOLVO_TARGET_VIN_SHA256"))
            if smart_state != "off":
                revoke("Volvo Smart Charging: " + smart_reason)
                return {"state": "inhibited", "writes": 0,
                        "reason": smart_reason, "volvo_smart_charging": smart_state}
            # A pause may only be resumed when THIS writer verified its FC10,
            # in the same independently observed EVCC cable session.
            cable_path = os.environ.get("EVCC_CABLE_TRACK_PATH")
            if not cable_path:
                revoke("mandatory EVCC cable source not configured")
                return {"state": "inhibited", "writes": 0,
                        "reason": "EVCC cable connection unverified"}
            try:
                cable = cable_tracker.read_fresh_connected(Path(cable_path), now=now)
            except cable_tracker.CableObservationError:
                cable = None
            if cable is None:
                revoke("EVCC cable disconnected, transitioning or observation stale")
                return {"state": "inhibited", "writes": 0,
                        "reason": "EVCC cable connection unverified"}
            if owner is not None and owner["state"] in ("pending", "releasing", "revoked"):
                return {"state": "inhibited", "writes": 0,
                        "reason": "unresolved or revoked price pause; reconciliation required"}
            if owner is not None and owner["state"] == "owned" and not ownership.is_owned(
                    owner, session=session, cable=cable, now=now):
                changed = (owner["evcc_cable_epoch"] != cable["connection_epoch"]
                           or owner["cable_coverage_epoch"] != cable["coverage_epoch"]
                           or owner["cable_tracker_id"] != cable["tracking_id"])
                revoke("EVCC cable continuity or session changed")
                return {"state": "inhibited", "writes": 0,
                        "reason": ("old price-pause ownership invalidated by cable epoch"
                                   if changed else "price pause ownership revoked; session changed")}
            try:
                evcc = json.loads(Path(os.environ.get("EVCC_READONLY_PATH", str(EVCC))).read_text())
                live = json.loads(Path(os.environ.get("INVERTER_DATA_PATH", str(LIVE))).read_text())
            except (OSError, ValueError, TypeError):
                revoke("EVCC or grid telemetry lost")
                return {"state": "inhibited", "writes": 0, "reason": "unreadable input snapshot"}
            # An externally resumed EVCC is a manual intervention, not a new
            # price-controlled pause opportunity in this connection session.
            if owner is not None and owner["state"] == "owned" and (
                    not isinstance(evcc, dict) or evcc.get("configured_pwm_raw") != RAW_PAUSE):
                revoke("EVCC PWM changed outside the price controller")
                return {"state": "inhibited", "writes": 0, "reason": "external EVCC change"}
            command = choose_action(
                session=session, price=price, evcc=evcc, live=live, now=now,
                price_pause_owned=ownership.is_owned(
                    owner, session=session, cable=cable, now=now))
            if command.action == "inhibited":
                revoke(command.reason)
            if command.action not in ("pause", "resume"):
                return {"state": command.action, "writes": 0, "reason": command.reason}
            host = os.environ.get("EVCC_HOST")
            if not host:
                revoke("EVCC address unavailable")
                return {"state": "inhibited", "writes": 0, "reason": "EVCC address not configured"}
            # The owner lock serializes price decisions; EVCC FC03 and FC10
            # additionally share the existing physical serial reader lock.
            with Path(os.environ.get("EVCC_READER_LOCK", str(LOCK))).open("a+b") as lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return {"state": "inhibited", "writes": 0,
                            "reason": "EVCC observation owns serial lock"}
                try:
                    # Re-read the durable cable file while holding both locks.
                    # A pending disconnect/replug invalidates an earlier preflight.
                    try:
                        latest = cable_tracker.read_fresh_connected(
                            Path(cable_path), now=now)
                    except cable_tracker.CableObservationError:
                        latest = None
                    if not cable_matches_locked_preflight(cable, latest, evcc):
                        revoke("EVCC cable observation changed before FC10")
                        return {"state": "inhibited", "writes": 0,
                                "reason": "cable state changed during FC10 preflight"}
                    # A web edit is an atomic intent replacement. A decision
                    # made against the old settings must not send FC10 after
                    # the settings revision changes during this invocation.
                    if session.get("energy_quality") == empirical.QUALITY:
                        try:
                            current_settings = load_settings(str(settings_path))
                        except (OSError, ValueError):
                            return {"state": "inhibited", "writes": 0,
                                    "reason": "EV settings became unreadable before FC10"}
                        if not intent_matches_locked_preflight(current_settings, session):
                            return {"state": "inhibited", "writes": 0,
                                    "reason": "EV charging preference changed before FC10"}
                    return execute_locked_fc10(
                        command=command, owner=owner, owner_path=owner_path,
                        session=session, cable=latest, now=now, host=host,
                    )
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        finally:
            fcntl.flock(owner_lock.fileno(), fcntl.LOCK_UN)


def publish_controller_status(result: dict, *, now: datetime | None = None) -> None:
    """Publish sanitized control/ownership status to the web UI, no commands.

    The EVCC pause-owner record remains private (0600, owner-only). The web
    user sees only a fail-closed summary, never owner/session identifiers.
    """
    now = now or datetime.now(timezone.utc)
    path = Path(os.environ.get("EV_PRICE_STATUS_PATH",
        "/opt/solinteg/ev-shadow-runtime/var/ev-price-controller-status.json"))
    if not path.parent.is_dir() or path.is_symlink():
        raise OSError("invalid price status destination")
    try:
        record = ownership.read_owner(Path(os.environ.get(
            "EV_PRICE_PAUSE_OWNER_PATH", str(OWNER))))
        owner_state = record["state"] if record else "none"
    except ownership.OwnershipError:
        owner_state = "unknown"
    mode = None
    departure = None
    revision = None
    try:
        ledger_path = Path(os.environ.get("EV_PRICE_EMPIRICAL_LEDGER_PATH",
            "/opt/solinteg/ev-shadow-runtime/var/ev-price-empirical-session.json"))
        ledger = empirical._ledger_read(ledger_path)
        if ledger is not None:
            mode = ledger["charge_mode"]
            departure = ledger["departure_at_utc"]
            revision = ledger.get("settings_revision")
    except (OSError, ValueError, TypeError, KeyError):
        pass
    output = {
        "schema_version": 1,
        "source": "evcc_price_controller_status",
        "timestamp": now.isoformat(),
        "control_enabled": (os.environ.get("EV_PRICE_CONTROL_ENABLED") == "1"
                            and os.environ.get("EVCC_PRICE_CONTROL_ARMED") == "1"),
        "state": result.get("state"),
        "reason": str(result.get("reason") or "")[:180],
        "writes": result.get("writes") if result.get("writes") in (0, 1) else None,
        "owner_state": owner_state,
        "effective_charge_mode": mode,
        "effective_departure_at_utc": departure,
        "effective_settings_revision": revision,
    }
    fd, name = tempfile.mkstemp(prefix=".ev-price-controller-status-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            os.fchmod(out.fileno(), 0o640)
            json.dump(output, out, sort_keys=True)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        dirfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dirfd)
        finally:
            os.close(dirfd)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", required=True)
    parser.parse_args()
    try:
        result = run_once()
    except (EvccError, EvccUncertainWrite, OSError, ValueError, PermissionError) as exc:
        # No blind retry and no claims about whether the write took effect.
        result = {"state": "error", "writes": None, "reason": type(exc).__name__}
    try:
        publish_controller_status(result)
    except OSError:
        # Telemetry failure never changes a physical FC10 outcome.
        pass
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
