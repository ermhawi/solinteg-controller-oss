"""Offline-only Solinteg Modbus broker core; no transport or live entry point.

Only a caller-supplied fake transport is accepted in phases 0-2. The installed
poller/dispatcher/watchdog are NOT connected to this module. In particular,
this module never imports pymodbus, opens a TCP socket or reads SOLINTEG_HOST.

An eventual hardware transport and independent fenced watchdog require a
separately approved cutover (docs/plans/modbus-broker-2026-11-01.md).
"""
from __future__ import annotations

import logging
import math
import queue
import threading
import time
import uuid
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from datetime import datetime, timezone
from typing import Callable

log = logging.getLogger(__name__)

GENERAL = 0x101
EMS_BATTCTRL = 0x303
GRID_CAP_RAW = 1100  # 11 kW inverter AC cap; NOT a site/fuse limit
READS = {"status", "read_snapshot"}
CONTROLS = {"charge", "discharge", "reserve_hold", "auto"}
SAFETY = {"safety_auto"}
ROLES = {
    "read": READS,
    "control": READS | CONTROLS,
    "safety": READS | SAFETY,
}
WORK_MODE_NAMES = {
    0x101: "General", 0x102: "Economic", 0x103: "UPS",
    0x104: "PeakShift", 0x105: "Feed-In", 0x200: "Off-Grid",
    0x301: "EMS ACCtrl", 0x302: "EMS General", 0x303: "EMS BattCtrl",
    0x304: "EMS Off-Grid", 0x400: "ToU",
}
SAMPLE_MAX_S = 20.0  # provisional, configurable; not approved for live gating
MAX_REQUEST_ID = 64
MAX_HISTORY = 100


class BrokerError(RuntimeError):
    pass


class UnsafeSample(BrokerError):
    pass


class Fenced(BrokerError):
    pass


def _s32(high: int, low: int) -> int:
    value = (high << 16) | low
    return value - 0x100000000 if value & 0x80000000 else value


def _u32(high: int, low: int) -> int:
    return (high << 16) | low


def assess_sample(sample: dict, *, max_span_s: float = SAMPLE_MAX_S) -> str:
    """Return a conservative quality code, not a physical-atomicity guarantee.

    Thresholds are hypotheses pending live read-only calibration. In particular
    a register readback is NEVER proof that a commanded power flow occurred.
    """
    if sample["mode_before"] != sample["mode_after"]:
        return "mode_changed"
    if sample["duration_s"] > max_span_s:
        return "stale"
    phases = (sample["meter_l1_w"], sample["meter_l2_w"], sample["meter_l3_w"])
    if any(phase is None for phase in phases):
        return "ct_invalid"
    grid = sample["grid_w"]
    if abs(sum(phases) - grid) > max(30, abs(grid) * .05):
        return "ct_invalid"
    battery, ac, pv = sample["battery_w"], sample["inverter_ac_w"], sample["pv_w"]
    # Observed historic negative battery power = charging, positive = discharge.
    # This heuristic does not substitute for temporal alignment/model validation.
    if abs(ac - (pv + battery)) > max(300, .15 * max(abs(ac), abs(pv) + abs(battery))):
        return "energy_balance_invalid"
    return "valid"


class Broker:
    """One worker owns the injected transport; requests can arrive concurrently.

    Explicitly NOT a production safety controller: default disarmed, optional
    phase guard must be supplied, and no real transport factory is provided.
    Any live adapter would require independent watchdog/fencing validation.
    """

    def __init__(
        self,
        transport_factory: Callable[[], object],
        *,
        armed: bool = False,
        phase_guard: Callable[[dict, str, int], bool] | None = None,
        min_write_interval_s: float = 2.0,
        max_sample_s: float = SAMPLE_MAX_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        allow_hold: bool = False,
        soc_floor_pct: float = 8.0,
        soc_ceiling_pct: float = 98.0,
        battery_kwh: float = 25.6,
        charge_sign: str | None = None,
        rejection_sink: Callable[[dict], None] | None = None,
    ):
        if not callable(transport_factory):
            raise TypeError("transport_factory required")
        if min_write_interval_s < 0 or max_sample_s <= 0 or battery_kwh <= 0:
            raise ValueError("invalid timing or battery capacity")
        if charge_sign not in (None, "neg_charge", "pos_charge"):
            raise ValueError("invalid confirmed 50207 polarity")
        self._factory = transport_factory
        self._transport = None
        self._armed = bool(armed)
        self._phase_guard = phase_guard
        self._interval = min_write_interval_s
        self._max_sample_s = max_sample_s
        self._clock = clock
        self._sleep = sleep
        self._allow_hold = allow_hold
        self._floor = soc_floor_pct
        self._ceiling = soc_ceiling_pct
        self._battery_kwh = battery_kwh
        self._charge_sign = charge_sign  # None refuses force_charge/discharge
        self._rejection_sink = rejection_sink
        self._boot_id = uuid.uuid4().hex[:12]
        self._sample_serial = 0
        self._lock = threading.Lock()
        self._tasks: queue.PriorityQueue = queue.PriorityQueue()
        self._order = 0
        self._generation = 0
        self._safety_latched = False
        self._closed = False
        self._requests: dict[tuple[str, str], tuple[tuple, Future]] = {}
        self._last_valid: dict | None = None
        self._rejected: list[dict] = []
        self._last_write_at: float | None = None
        self._owns_control = False
        self._worker = threading.Thread(target=self._run, daemon=True, name="offline-modbus-broker")
        self._worker.start()

    def status(self) -> dict:
        with self._lock:
            return {
                "armed": self._armed,
                "safety_latched": self._safety_latched,
                "generation": self._generation,
                "queue_depth": self._tasks.qsize(),
                "last_valid_timestamp": (self._last_valid or {}).get("timestamp"),
                "rejected_samples": len(self._rejected),
                "last_rejection": self._rejected[-1]["quality"] if self._rejected else None,
                "owns_forced_state": self._owns_control,
                "offline_only": True,
            }

    def submit(
        self, operation: str, *, role: str = "read",
        request_id: str | None = None, watts: int | None = None,
        timeout_s: float = 30.0,
    ) -> dict:
        if operation not in ROLES.get(role, frozenset()):
            raise PermissionError("operation is not authorized for peer role")
        if type(timeout_s) not in (float, int) or not math.isfinite(timeout_s) or not 0 < timeout_s <= 120:
            raise ValueError("timeout_s must be finite and in (0, 120]")
        if operation == "status":
            return self.status()

        control = operation in CONTROLS | SAFETY
        if control:
            if not isinstance(request_id, str) or not request_id or len(request_id) > MAX_REQUEST_ID:
                raise ValueError("control request needs a nonempty bounded request_id")
            if not all(c.isascii() and (c.isalnum() or c in "._:-") for c in request_id):
                raise ValueError("invalid request_id")
            if not self._armed:
                raise PermissionError("broker is disarmed")
        if operation in {"charge", "discharge", "reserve_hold"}:
            if type(watts) is not int or not 0 < watts <= 11000 or watts % 10:
                raise ValueError("power must be 10..11000 W in steps of 10")
        elif watts is not None:
            raise ValueError("watts only allowed for power actions")

        with self._lock:
            if self._closed:
                raise BrokerError("broker stopped")
            key = (operation, watts, role)
            idempotency_key = (role, request_id)
            if control and idempotency_key in self._requests:
                old_key, future = self._requests[idempotency_key]
                if key != old_key:
                    raise BrokerError("request_id reused for different operation")
            else:
                if operation in CONTROLS and self._safety_latched:
                    raise Fenced("safety latch prevents new control")
                if control and operation not in SAFETY and len(self._requests) >= 1024:
                    # Never queue a command before reserving its idempotency key.
                    # Safety requests always bypass this cap; exhausted normal
                    # idempotency history must never block a watchdog revert.
                    # Eviction is safe only for a previously COMPLETED request.
                    old = next((k for k, (_, f) in self._requests.items() if f.done()), None)
                    if old is None:
                        raise BrokerError("idempotency cache full")
                    del self._requests[old]
                if operation in SAFETY:
                    # Fence queued AND in-flight non-safety requests immediately.
                    self._generation += 1
                    self._safety_latched = True
                epoch = self._generation
                future = Future()
                self._order += 1
                rank = 0 if operation in SAFETY else 10 if operation in CONTROLS else 20
                if control:
                    self._requests[idempotency_key] = (key, future)
                self._tasks.put((rank, self._order, epoch, operation, watts, future))

        try:
            return future.result(timeout=timeout_s)
        except FutureTimeoutError as exc:
            if control:
                with self._lock:
                    # An ambiguous client timeout MUST NOT leave a queued write valid.
                    self._generation += 1
                    self._safety_latched = True
            raise TimeoutError("broker request result unknown; control fenced") from exc

    def _fenced(self, epoch: int) -> None:
        with self._lock:
            if epoch != self._generation:
                raise Fenced("superseded by safety request or timeout")

    def _client(self):
        if self._transport is not None and not getattr(self._transport, "connected", False):
            self._transport.close()
            self._transport = None
        if self._transport is None:
            self._transport = self._factory()
            if not self._transport.connect():
                self._transport.close()
                self._transport = None
                raise BrokerError("fake transport refused connection")
        return self._transport

    def _read(self, addr: int, count: int, blocks: list | None = None) -> list[int]:
        begin = self._clock()
        try:
            result = self._client().read_holding_registers(addr, count=count, device_id=255)
            if result.isError() or len(result.registers) != count:
                raise BrokerError(f"FC03 read failed at {addr}")
            return list(result.registers)
        finally:
            if blocks is not None:
                blocks.append({"address": addr, "count": count,
                               "start_s": round(begin, 3), "end_s": round(self._clock(), 3)})

    def _snapshot(self) -> dict:
        start = self._clock()
        self._sample_serial += 1
        sample_id = f"{self._boot_id}-{self._sample_serial}"
        blocks: list[dict] = []
        sample: dict = {"sample_id": sample_id}
        try:
            sample["mode_before"] = self._read(50000, 1, blocks)[0]
            try:
                regs = self._read(10994, 36, blocks)
                phases = (_s32(regs[0], regs[1]), _s32(regs[2], regs[3]),
                          _s32(regs[4], regs[5]))
                offset = 6
            except (OSError, BrokerError):
                regs = self._read(11000, 30, blocks)
                phases = (None, None, None)
                offset = 0
            battery = self._read(30258, 2, blocks)
            soc = self._read(33000, 4, blocks)
            sample["mode_after"] = self._read(50000, 1, blocks)[0]
            try:
                balance = self._read(50006, 1, blocks)[0]
            except (OSError, BrokerError):
                balance = None
            sample.update({
                "meter_l1_w": phases[0], "meter_l2_w": phases[1],
                "meter_l3_w": phases[2],
                "grid_w": _s32(regs[offset], regs[offset+1]),
                "inverter_ac_w": _s32(regs[offset+16], regs[offset+17]),
                "pv_w": _u32(regs[offset+28], regs[offset+29]),
                "battery_w": _s32(*battery), "soc_pct": round(soc[0] * .01, 2),
                "soh_pct": round(soc[1] * .01, 2),
                "battery_temp_c": round(soc[3] * .1, 1),
                "work_mode_raw": sample["mode_after"],
                "work_mode": WORK_MODE_NAMES.get(
                    sample["mode_after"], f"Unknown(0x{sample['mode_after']:X})"),
                "grid_unbalanced_output_raw": balance,
            })
            sample["house_load_w"] = sample["inverter_ac_w"] - sample["grid_w"]
            sample["soc_kwh"] = round(sample["soc_pct"] / 100 * self._battery_kwh, 3)
            sample["duration_s"] = round(self._clock() - start, 3)
            quality = assess_sample(sample, max_span_s=self._max_sample_s)
        except (OSError, BrokerError, TimeoutError, ValueError) as exc:
            quality = "timeout"
            sample["duration_s"] = round(self._clock() - start, 3)
            sample["error_type"] = type(exc).__name__

        sample["blocks"] = [{"address": b["address"], "count": b["count"],
                             "start_s": round(b["start_s"] - start, 3),
                             "end_s": round(b["end_s"] - start, 3)} for b in blocks]
        sample["quality"] = quality
        if quality != "valid":
            diagnostic = {"sample_id": sample_id, "quality": quality,
                          "duration_s": sample["duration_s"], "blocks": sample["blocks"],
                          "timestamp": datetime.now(timezone.utc).isoformat()}
            self._rejected.append(diagnostic)
            self._rejected = self._rejected[-MAX_HISTORY:]
            if self._rejection_sink is not None:
                try:
                    self._rejection_sink(dict(diagnostic))
                except Exception:  # noqa: BLE001 - never publish an invalid sample on sink failure
                    log.exception("offline rejection diagnostic sink failed")
            return {"status": "rejected", "reason": quality, "sample": None,
                    "sample_id": sample_id, "duration_s": sample["duration_s"],
                    "blocks": sample["blocks"]}
        sample["timestamp"] = datetime.now(timezone.utc).isoformat()
        self._last_valid = sample
        return {"status": "valid", "sample": sample}

    def _write(self, addr: int, value: int, epoch: int | None) -> None:
        if epoch is not None:
            self._fenced(epoch)
        now = self._clock()
        if self._last_write_at is not None and now-self._last_write_at < self._interval:
            self._sleep(self._interval - (now-self._last_write_at))
        if epoch is not None:
            self._fenced(epoch)
        raw = value & 0xFFFF
        # Never retry an ambiguous FC06 write: readback/rollback are separate.
        self._last_write_at = self._clock()
        reply = self._client().write_register(addr, raw, device_id=255)
        if reply.isError():
            raise BrokerError(f"FC06 write failed at {addr}")
        if self._read(addr, 1)[0] != raw:
            raise BrokerError(f"FC06 readback mismatch at {addr}")

    def _return_auto(self) -> None:
        """Best-effort all steps; mode attempted even if cap restore fails."""
        errors = []
        for addr, value in ((50208, GRID_CAP_RAW), (50209, -GRID_CAP_RAW),
                            (50000, GENERAL), (50207, 0)):
            try:
                self._write(addr, value, epoch=None)
            except Exception as exc:  # noqa: BLE001 - safety must attempt later steps
                errors.append(f"{addr}:{type(exc).__name__}")
        try:
            if self._read(50000, 1)[0] != GENERAL:
                errors.append("mode_not_auto")
        except Exception:
            errors.append("mode_unverified")
        if errors:
            raise BrokerError("auto restore not verified: " + ",".join(errors))

    def _apply(self, operation: str, watts: int | None, epoch: int) -> dict:
        self._fenced(epoch)
        if operation in {"auto", "safety_auto"}:
            if operation == "auto" and not self._owns_control:
                # Preserve an operator-selected mode; matching dispatch_loop's
                # idle->idle no-op. Safety watchdog remains able to force Auto.
                return {"status": "unowned_noop", "action": operation}
            self._return_auto()
            self._owns_control = False
            return {"status": "registers_verified_only", "action": operation, "mode": GENERAL}

        check = self._snapshot()
        if check["status"] != "valid":
            raise UnsafeSample("preflight rejected: " + check["reason"])
        sample = check["sample"]
        if sample["work_mode_raw"] not in (GENERAL, EMS_BATTCTRL):
            raise UnsafeSample("unexpected inverter working mode")
        if sample["work_mode_raw"] == EMS_BATTCTRL and not self._owns_control:
            raise UnsafeSample("EMS mode not owned by this broker")
        if operation in {"charge", "discharge"} and self._charge_sign is None:
            raise UnsafeSample("50207 sign convention not confirmed")
        if self._phase_guard is None or not self._phase_guard(sample, operation, watts):
            raise UnsafeSample("required physical phase guard not satisfied")
        if operation == "charge" and sample["soc_pct"] >= self._ceiling:
            raise UnsafeSample("SoC at charge ceiling")
        if operation == "discharge" and sample["soc_pct"] <= self._floor:
            raise UnsafeSample("SoC at discharge floor")
        if operation == "reserve_hold" and (not self._allow_hold or sample["pv_w"] > 0):
            raise UnsafeSample("hold unvalidated or PV present")
        if operation == "reserve_hold":
            writes = [(50208, GRID_CAP_RAW), (50209, -GRID_CAP_RAW), (50210, 1),
                      (50207, 0), (50000, EMS_BATTCTRL)]
        else:
            charge = operation == "charge"
            charge_dir = -1 if self._charge_sign == "neg_charge" else 1
            target = (watts // 10) * charge_dir * (1 if charge else -1)
            writes = [(50209, -GRID_CAP_RAW), (50208, GRID_CAP_RAW),
                      (50210, 0 if charge else 1),
                      (50207, target), (50000, EMS_BATTCTRL)]
        began = False
        try:
            for addr, value in writes:
                began = True
                self._owns_control = True
                self._write(addr, value, epoch)
            self._fenced(epoch)  # safety may arrive immediately after final readback
            return {"status": "registers_verified_only", "action": operation,
                    "mode": EMS_BATTCTRL, "physical_effect_verified": False}
        except Exception as exc:
            if began:
                # A half-applied control must not be followed by a fresh
                # dispatcher command, even when best-effort Auto succeeds.
                with self._lock:
                    # Never fence out a newer safety_auto already in the queue.
                    # Only mint a new fence if this transaction is still current.
                    if self._generation == epoch:
                        self._generation += 1
                    self._safety_latched = True
                try:
                    self._return_auto()
                    self._owns_control = False
                except Exception as revert_exc:
                    raise BrokerError(f"transaction aborted ({type(exc).__name__}); "
                                      f"rollback FAILED: {revert_exc}") from exc
                raise BrokerError(f"transaction aborted ({type(exc).__name__}); "
                                  "Auto rollback register-verified only") from exc
            raise

    def _run(self):
        while True:
            rank, order, epoch, operation, watts, future = self._tasks.get()
            try:
                if operation == "_stop":
                    # Clean worker exit uses the SAME serial Modbus owner, not
                    # a competing client. Leave operator-selected modes alone.
                    try:
                        if self._armed and self._owns_control:
                            self._return_auto()
                            self._owns_control = False
                        future.set_result(None)
                    except Exception as exc:  # noqa: BLE001
                        future.set_exception(exc)
                    return
                self._fenced(epoch)
                result = self._snapshot() if operation == "read_snapshot" else self._apply(
                    operation, watts, epoch)
                future.set_result(result)
            except Exception as exc:  # noqa: BLE001 - broker returns explicit failure
                future.set_exception(exc)
            finally:
                self._tasks.task_done()

    def close(self, timeout_s: float = 30.0) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._generation += 1
            # Resolve all queued futures rather than orphaning client waiters
            # behind the prioritized shutdown sentinel.
            while True:
                try:
                    _, _, _, _, _, pending = self._tasks.get_nowait()
                except queue.Empty:
                    break
                pending.set_exception(BrokerError("broker stopped; queued work cancelled"))
                self._tasks.task_done()
            done = Future()
            self._order += 1
            self._tasks.put((-1, self._order, self._generation, "_stop", None, done))
        error = None
        try:
            done.result(timeout=timeout_s)
        except Exception as exc:  # noqa: BLE001 - still close owned transport
            error = exc
        self._worker.join(timeout=timeout_s)
        if self._worker.is_alive():
            raise TimeoutError("offline broker worker did not stop")
        if self._transport is not None:
            self._transport.close()
            self._transport = None
        if error is not None:
            raise BrokerError("worker shutdown Auto not verified") from error
