"""Complete legacy register transactions on the broker's single Modbus worker.

The existing inverter_control algorithms are the only implementation of
charge/discharge/hold/Auto and import-limit sequences. The dispatcher sends a
single request; the broker does ALL checks, writes, delays and readbacks before
the next poller request can use the TCP connection.

This is not a new failure supervisor. The caller retains the existing
best-effort revert behavior, including its documented limitations.
"""
from __future__ import annotations

import logging

import inverter_control as control

log = logging.getLogger("solinteg.broker.transactions")


class _WorkerModbusClient:
    """Inverter-compatible view of the broker-owned transport; never closes it."""

    def __init__(self, broker):
        self._broker = broker
        self._epoch = None

    @property
    def connected(self):
        return True

    def connect(self):
        return True

    def _owned(self):
        if self._epoch is None:
            raise RuntimeError("broker worker transport used outside a transaction")
        self._broker._fenced(self._epoch)
        return self._broker._client()

    def read_holding_registers(self, address, *, count=1, device_id=255):
        return self._owned().read_holding_registers(
            address, count=count, device_id=device_id,
        )

    def write_register(self, address, value, *, device_id=255):
        return self._owned().write_register(
            address, value, device_id=device_id,
        )

    def close(self):
        # The physical connection is owned by the broker, never the caller.
        pass


class LegacyTransactionWorker:
    """One persistent Inverter/RateLimiter instance, only used by Broker._run."""

    def __init__(self, broker):
        if not broker._armed or not control.ARMED:
            raise PermissionError("legacy transactions require existing physical arm")
        if control.SIGN != broker._charge_sign:
            raise PermissionError("dispatcher and broker charge polarity mismatch")
        if control.SLAVE_ID != 255:
            raise PermissionError("legacy transaction unit must be 255")
        self._client = _WorkerModbusClient(broker)
        self._inverter = control.Inverter(unit=255, client=self._client)

    def execute(self, operation: str, power: int | None,
                min_soc_pct: float | None, epoch: int) -> dict:
        if self._client._epoch is not None:
            raise RuntimeError("nested broker transaction")
        self._client._epoch = epoch
        try:
            self._client._broker._fenced(epoch)
            if operation == "transaction_charge":
                control.force_charge(self._inverter, power)
            elif operation == "transaction_discharge":
                control.force_discharge(self._inverter, power)
            elif operation == "transaction_hold":
                held = control.force_hold(self._inverter, min_soc_pct)
                self._client._broker._fenced(epoch)
                log.info("complete transaction %s applied (held=%s)", operation, held)
                return {"status": "applied", "held": held}
            elif operation == "transaction_auto":
                control.return_to_auto(self._inverter)
            elif operation == "transaction_freeze":
                control.freeze_battery(self._inverter, min_soc_pct)
            elif operation == "transaction_import_limit":
                control.set_import_limit(self._inverter, power)
            elif operation == "transaction_clear_import_limit":
                control.clear_import_limit(self._inverter)
            else:
                raise ValueError("unsupported legacy broker transaction")
            self._client._broker._fenced(epoch)
            log.info("complete transaction %s applied", operation)
            return {"status": "applied"}
        except Exception as exc:  # noqa: BLE001 - keep legacy exception/failure contract
            log.warning("complete transaction %s failed (%s)",
                        operation, type(exc).__name__)
            raise
        finally:
            self._client._epoch = None
