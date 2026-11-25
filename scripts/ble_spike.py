"""Hardware check: scan, connect, take control, hold a target, print telemetry, stop.

python scripts/ble_spike.py [--addr ADDR] [--watts 150] [--seconds 30]
python scripts/ble_spike.py --report [--addr ADDR] [--seconds 30]

--report controls nothing: it prints what a trainer, strap or power meter offers and sends,
to support devices we do not have. Addresses and numbers in names are masked.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections.abc import Awaitable
from typing import Any

from pywatts.trainer.base import ConnectionState, Telemetry, TrainerEvent
from pywatts.trainer.cycling_power import CP_MEASUREMENT, CP_SERVICE
from pywatts.trainer.ftms import (
    BIKE_DATA,
    FEATURES,
    FTMS_SERVICE,
    HR_MEASUREMENT,
    HR_SERVICE,
    MACHINE_STATUS,
    POWER_RANGE,
    FtmsTrainer,
    describe_error,
    ftms_uuid,
)


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


REPORT_READS = {
    "features": FEATURES,
    "power range": POWER_RANGE,
    "power meter features": ftms_uuid(0x2A65),
    "maker": ftms_uuid(0x2A29),
    "model": ftms_uuid(0x2A24),
    "firmware": ftms_uuid(0x2A26),
    "battery": ftms_uuid(0x2A19),
}
REPORT_NOTIFY = {
    "bike data": BIKE_DATA,
    "status": MACHINE_STATUS,
    "power": CP_MEASUREMENT,
    "heart rate": HR_MEASUREMENT,
}
SAMPLES = 8  # of each kind


def mask(name: str | None) -> str:
    return re.sub(r"[0-9A-F]{3,}", "***", name or "no name", flags=re.IGNORECASE)


async def report(addr: str | None, seconds: float) -> int:
    from bleak import BleakClient, BleakScanner

    wanted = {FTMS_SERVICE, CP_SERVICE, HR_SERVICE}
    found = await step("scan", BleakScanner.discover(timeout=8.0, return_adv=True))
    devices = []
    for dev, adv in found.values():
        uuids = {u.lower() for u in (*adv.service_uuids, *adv.service_data)}
        if dev.address == addr or (addr is None and uuids & wanted):
            devices.append((dev, adv))
    for number, (dev, adv) in enumerate(sorted(devices, key=lambda d: -d[1].rssi), 1):
        print(f"\nDevice {number}: {mask(adv.local_name or dev.name)}, {adv.rssi} dBm")
        print(f"  advertises: {', '.join(sorted(adv.service_uuids)) or 'no services'}")
        try:
            async with BleakClient(dev) as client:
                for service in client.services:
                    print(f"  service {service.uuid}")
                    for char in service.characteristics:
                        print(f"    {char.uuid}  {' '.join(sorted(char.properties))}")
                for name, uuid in REPORT_READS.items():
                    if client.services.get_characteristic(uuid) is not None:
                        try:
                            print(f"  {name}: {bytes(await client.read_gatt_char(uuid)).hex(' ')}")
                        except Exception as e:
                            print(f"  {name}: not readable, {describe_error(e)}")
                counts: dict[str, int] = {}

                def show(name: str, data: bytearray, counts: dict[str, int] = counts) -> None:
                    counts[name] = counts.get(name, 0) + 1
                    if counts[name] <= SAMPLES:
                        print(f"  {name}: {data.hex(' ')}", flush=True)

                for name, uuid in REPORT_NOTIFY.items():
                    if client.services.get_characteristic(uuid) is not None:
                        try:
                            await client.start_notify(uuid, lambda _c, d, name=name: show(name, d))
                        except Exception as e:
                            print(f"  {name}: no notifications, {describe_error(e)}")
                print(f"  listening for {seconds:.0f} s, please pedal")
                await asyncio.sleep(seconds)
                print(f"  received: {counts or 'nothing'}")
        except Exception as e:
            fail(describe_error(e))
    if not devices:
        fail("no trainer, power meter or heart rate strap found")
    return 0 if devices else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="FTMS trainer hardware check")
    parser.add_argument("--addr", help="device address; default: strongest FTMS device found")
    parser.add_argument("--watts", type=int, default=150)
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--report", action="store_true", help="describe devices, control nothing")
    args = parser.parse_args()
    try:
        if args.report:
            return asyncio.run(report(args.addr, args.seconds))
        return asyncio.run(run(args.addr, args.watts, args.seconds))
    except StepFailed:
        return 1
    except KeyboardInterrupt:
        fail("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
