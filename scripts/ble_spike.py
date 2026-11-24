"""Hardware check: scan, connect, take control, hold a target, print telemetry, stop.

python scripts/ble_spike.py [--addr ADDR] [--watts 150] [--seconds 30]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable
from typing import Any

from pywatts.trainer.base import ConnectionState, Telemetry, TrainerEvent
from pywatts.trainer.ftms import FtmsTrainer


class StepFailed(Exception):
    pass


def ok(text: str) -> None:
    print(f"[OK]   {text}", flush=True)


def fail(text: str) -> None:
    print(f"[FAIL] {text}", flush=True)


async def step(name: str, awaitable: Awaitable[Any]) -> Any:
    try:
        result = await awaitable
    except Exception as e:
        fail(f"{name}: {e or type(e).__name__}")
        raise StepFailed from e
    ok(name)
    return result


async def run(addr: str | None, watts: int, seconds: float) -> int:
    trainer = FtmsTrainer()
    samples: list[Telemetry] = []
    show = False
    states: list[ConnectionState] = []

    def listen(event: TrainerEvent) -> None:
        if isinstance(event, ConnectionState):
            states.append(event)
            print(f"       state: {event.value}", flush=True)
        elif show:
            samples.append(event)
            print(
                f"       power={event.power_w} W  cadence={event.cadence_rpm} rpm  "
                f"speed={event.speed_kph} km/h  hr={event.hr_bpm}",
                flush=True,
            )

    trainer.add_listener(listen)
    connected = False
    code = 1
    try:
        if addr is None:
            devices = await step("scan", trainer.scan(8.0))
            for d in devices:
                print(f"       {d.name}  {d.addr}  {d.rssi} dBm")
            if not devices:
                fail("no FTMS trainer found (awake? not connected to another app?)")
                raise StepFailed
            addr = devices[0].addr
        else:
            ok(f"scan skipped, using {addr}")
        await step(f"connect {addr}", trainer.connect(addr))
        connected = True
        features = await step("read features", trainer.read_features())
        if features is None:
            print("       not readable")
        else:
            print(f"       machine=0x{features.machine:08x} target=0x{features.target:08x}")
            print(
                f"       power target: {features.power_target}  "
                f"open mode: {features.simulation}  "
                f"power: {features.power_measurement}  cadence: {features.cadence}"
            )
        await step("request control", trainer.request_control())
        await step(f"set target {watts} W", trainer.set_target_power(watts))
        show = True
        await asyncio.sleep(seconds)
        show = False
        powers = [s.power_w for s in samples if s.power_w is not None]
        if ConnectionState.RECONNECTING in states:
            fail("connection dropped during the test")
        elif powers:
            ok(f"telemetry: {len(samples)} samples, mean {sum(powers) / len(powers):.0f} W")
            code = 0
        else:
            fail("telemetry: no power received")
    except StepFailed:
        pass
    finally:
        show = False
        for name, wanted, fn in (
            ("stop", connected, trainer.stop),
            ("disconnect", True, trainer.disconnect),
        ):
            if not wanted:
                continue
            if name == "stop" and states[-1] is not ConnectionState.CONNECTED:
                fail("stop: not connected, trainer may still hold resistance")
                code = 1
                continue
            try:
                await step(name, fn())
            except StepFailed:
                code = 1
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="FTMS trainer hardware check")
    parser.add_argument("--addr", help="device address; default: strongest FTMS device found")
    parser.add_argument("--watts", type=int, default=150)
    parser.add_argument("--seconds", type=float, default=30.0)
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.addr, args.watts, args.seconds))
    except KeyboardInterrupt:
        fail("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
