#!/usr/bin/env python3
"""Send one batch of commands to a DayBetter-style BLE LED lamp.

Usage:
    lampctl.py [--dry-run] [--config PATH] on
    lampctl.py [--dry-run] [--config PATH] off
    lampctl.py [--dry-run] [--config PATH] rgb R G B      (0-255 each)
    lampctl.py [--dry-run] [--config PATH] bright PCT     (1-100; the lamp clamps values below 10 up to 10)

--dry-run prints the frames as hex instead of connecting to the lamp (no Bluetooth needed).

Frame format: A0 <cmd> <len = len(payload) + 3> <payload...> <crc16 lo> <crc16 hi>
CRC-16/MODBUS (init 0xFFFF, reflected poly 0xA001, no final xor) over every byte before the CRC.
Frames are written to the write characteristic; replies arrive on the notify characteristic.

This is an independent implementation written from protocol facts. Protocol corrections were
informed by the MIT-licensed ma67-ex/ClaudeLamp project; no code was copied from it.
"""
import argparse
import asyncio
import fcntl
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG = os.path.join(HERE, "..", "config.json")

DEFAULTS = {
    "ble_name": "",
    "write_uuid": "0000a065-0000-1000-8000-00805f9b34fb",
    "notify_uuid": "0000f065-0000-1000-8000-00805f9b34fb",
    "lock_path": "~/.cache/ble-lamp-matter-bridge/ble.lock",
    "scan_timeout": 15,
}

CMD_POWER = 0x11
CMD_MODE = 0x12
CMD_BRIGHTNESS = 0x13
CMD_COLOR = 0x15
MODE_COLOR = b"\x01\x00"


def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def frame(cmd: int, payload: bytes) -> bytes:
    body = bytes([0xA0, cmd, len(payload) + 3]) + payload
    return body + crc16_modbus(body).to_bytes(2, "little")


def frames_for(command: str, values: list) -> list:
    """Build the frame batch for one command. The mode frame (0x12) must precede colour and
    brightness frames, otherwise the lamp ignores brightness."""
    if command == "on":
        return [frame(CMD_POWER, b"\x01")]
    if command == "off":
        return [frame(CMD_POWER, b"\x00")]
    if command == "rgb":
        if len(values) != 3:
            raise ValueError("rgb needs three values: R G B")
        r, g, b = (max(0, min(255, int(v))) for v in values)
        return [frame(CMD_POWER, b"\x01"), frame(CMD_MODE, MODE_COLOR), frame(CMD_COLOR, bytes([r, g, b, 0xFF]))]
    if command == "bright":
        if len(values) != 1:
            raise ValueError("bright needs one value: PCT")
        pct = max(1, min(100, int(values[0])))
        return [frame(CMD_POWER, b"\x01"), frame(CMD_MODE, MODE_COLOR), frame(CMD_BRIGHTNESS, bytes([pct]))]
    raise ValueError(f"unknown command: {command}")


def load_config(path: str) -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path) as fh:
            cfg.update(json.load(fh).get("lamp", {}))
    return cfg


async def send(cfg: dict, frames: list) -> None:
    from bleak import BleakClient, BleakScanner  # imported lazily so --dry-run and tests need no bleak

    name = cfg["ble_name"]
    dev = await BleakScanner.find_device_by_name(name, timeout=cfg["scan_timeout"])
    if dev is None:
        sys.exit(f"{name} not found. Close the vendor phone app (BLE allows one connection) "
                 "and power-cycle the lamp if it still does not appear.")
    async with BleakClient(dev, timeout=cfg["scan_timeout"]) as client:
        # Subscribe first and write WITH response: write-without-response silently drops brightness.
        await client.start_notify(cfg["notify_uuid"], lambda _h, _d: None)
        for f in frames:
            await client.write_gatt_char(cfg["write_uuid"], f, response=True)
            await asyncio.sleep(0.1)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print frames as hex, do not connect")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    ap.add_argument("command", choices=["on", "off", "rgb", "bright"])
    ap.add_argument("values", nargs="*")
    args = ap.parse_args(argv)

    try:
        frames = frames_for(args.command, args.values)
    except ValueError as exc:
        ap.error(str(exc))

    if args.dry_run:
        for f in frames:
            print(f.hex(" "))
        return 0

    cfg = load_config(args.config)
    if not cfg["ble_name"]:
        sys.exit("Set lamp.ble_name in config.json (see config.example.json).")
    lock_path = os.path.expanduser(cfg["lock_path"])
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    # One BLE connection at a time: concurrent callers queue on this lock instead of racing for the radio.
    with open(lock_path, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        asyncio.run(send(cfg, frames))
    return 0


if __name__ == "__main__":
    sys.exit(main())
