#!/usr/bin/env python3
"""Isolated 26->28% retry; delegate Modbus control to the pinned dispatcher.

This entrypoint changes no inverter/BMS/EVCC setting or installed source.
The original soc25_reserve_dispatch module provides the same reviewed logic.
"""
from pathlib import Path
import sys

ROOT_LIB = Path("/usr/local/libexec/solinteg-soc25-recovery-retry")
sys.path.insert(0, str(ROOT_LIB))

import soc25_reserve_dispatch as dispatch_wrapper


def main() -> None:
    dispatch_wrapper.ROOT_LIB = ROOT_LIB
    dispatch_wrapper.main()


if __name__ == "__main__":
    main()
