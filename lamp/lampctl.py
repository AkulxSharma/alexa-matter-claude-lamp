#!/usr/bin/env python3
"""Find, verify and control a DayBetter-style BLE LED lamp.

Setup (run once, before starting the Matter bridge):
    lampctl.py scan                      list nearby lamps (they all look alike, e.g. P065_xxxx)
    lampctl.py setup                     pick your lamp, blink it to prove it is yours, save it to config.json
    lampctl.py check                     connect, read the lamp state, exit 0 if the Mac can control it

Control (used by the bridge):
    lampctl.py on | off
    lampctl.py rgb R G B                 0-255 each
    lampctl.py bright PCT                1-100; the lamp clamps values below 10 up to 10

Options: --dry-run prints control frames as hex instead of connecting; --config PATH picks the config file.

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
import re
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(HERE, ".."))
DEFAULT_CONFIG = os.path.join(ROOT, "config.json")
EXAMPLE_CONFIG = os.path.join(ROOT, "config.example.json")

DEFAULTS = {
    "ble_name": "",
    "ble_address": "",
    "service_uuid": "0000c065-0000-1000-8000-00805f9b34fb",
    "write_uuid": "0000a065-0000-1000-8000-00805f9b34fb",
    "notify_uuid": "0000f065-0000-1000-8000-00805f9b34fb",
    "name_pattern": r"^P0[0-9A-Fa-f]{2}_[0-9A-Fa-f]{4}$",
    "lock_path": "~/.cache/ble-lamp-matter-bridge/ble.lock",
    "scan_timeout": 15,
}

CMD_STATE = 0x10
CMD_POWER = 0x11
CMD_MODE = 0x12
CMD_BRIGHTNESS = 0x13
CMD_COLOR = 0x15
MODE_COLOR = b"\x01\x00"


# ---------------------------------------------------------------- protocol (pure, tested)

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
    """Build the frame batch for one control command. The mode frame (0x12) must precede colour and
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


def parse_state(data: bytes):
    """Parse a read-state reply: A1 10 <len> on mode sub brightness speed sensitivity R G B ...
    Returns a dict, or None if the bytes are not a state reply."""
    if len(data) < 12 or data[0] != 0xA1 or data[1] != CMD_STATE:
        return None
    p = data[3:]
    return {"on": bool(p[0]), "mode": p[1], "brightness": p[3], "rgb": [p[6], p[7], p[8]]}


def is_candidate(name, service_uuids, cfg) -> bool:
    """A lamp is a candidate if it advertises the lamp service UUID or its name matches the pattern."""
    if cfg["service_uuid"].lower() in [u.lower() for u in service_uuids or []]:
        return True
    return bool(name and re.match(cfg["name_pattern"], name))


# ---------------------------------------------------------------- config

def load_config(path: str) -> dict:
    cfg = dict(DEFAULTS)
    if os.path.exists(path):
        with open(path) as fh:
            cfg.update(json.load(fh).get("lamp", {}))
    return cfg


def save_lamp(path: str, name: str, address: str) -> None:
    if not os.path.exists(path):
        shutil.copyfile(EXAMPLE_CONFIG, path)
    with open(path) as fh:
        data = json.load(fh)
    data.setdefault("lamp", {}).update({"ble_name": name, "ble_address": address})
    with open(path, "w") as fh:
        json.dump(data, fh, indent=2)
        fh.write("\n")


# ---------------------------------------------------------------- BLE

class Lamp:
    """One BLE session with the lamp. Subscribes to notifications and writes WITH response:
    write-without-response silently drops brightness commands."""

    def __init__(self, cfg, device):
        self.cfg, self.device = cfg, device
        self.client = None
        self.replies = asyncio.Queue()

    async def __aenter__(self):
        from bleak import BleakClient
        self.client = BleakClient(self.device, timeout=self.cfg["scan_timeout"])
        await self.client.connect()
        if self.client.services.get_characteristic(self.cfg["write_uuid"]) is None:
            await self.client.disconnect()
            raise RuntimeError("connected, but this device has no lamp write characteristic; wrong device or model")
        await self.client.start_notify(self.cfg["notify_uuid"], lambda _h, d: self.replies.put_nowait(bytes(d)))
        return self

    async def __aexit__(self, *exc):
        await self.client.disconnect()

    async def write(self, frames):
        for f in frames:
            await self.client.write_gatt_char(self.cfg["write_uuid"], f, response=True)
            await asyncio.sleep(0.1)

    async def read_state(self, timeout=3.0):
        while not self.replies.empty():
            self.replies.get_nowait()
        await self.write([frame(CMD_STATE, b"\x01")])
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while (left := deadline - loop.time()) > 0:
            try:
                state = parse_state(await asyncio.wait_for(self.replies.get(), left))
            except asyncio.TimeoutError:
                break
            if state:
                return state
        return None

    async def blink(self, times=3):
        """Toggle power a few times, then restore the original power state."""
        state = await self.read_state()
        was_on = state["on"] if state else True
        for _ in range(times):
            await self.write([frame(CMD_POWER, b"\x00" if was_on else b"\x01")])
            await asyncio.sleep(0.6)
            await self.write([frame(CMD_POWER, b"\x01" if was_on else b"\x00")])
            await asyncio.sleep(0.6)


async def scan(cfg, show_all=False):
    from bleak import BleakScanner
    found = await BleakScanner.discover(timeout=min(cfg["scan_timeout"], 10), return_adv=True)
    rows = []
    for device, adv in found.values():
        name = adv.local_name or device.name
        if show_all and name or is_candidate(name, adv.service_uuids, cfg):
            rows.append((name or "?", device.address, adv.rssi, device))
    return sorted(rows, key=lambda r: -r[2])  # strongest signal (usually closest) first


async def find_configured(cfg):
    from bleak import BleakScanner
    if not cfg["ble_name"] and not cfg["ble_address"]:
        sys.exit("No lamp configured. Run: lampctl.py setup")
    dev = None
    if cfg["ble_address"]:
        dev = await BleakScanner.find_device_by_address(cfg["ble_address"], timeout=cfg["scan_timeout"])
    if dev is None and cfg["ble_name"]:
        dev = await BleakScanner.find_device_by_name(cfg["ble_name"], timeout=cfg["scan_timeout"])
    if dev is None:
        sys.exit(f"Lamp {cfg['ble_name'] or cfg['ble_address']} not found. Fully close the vendor phone app "
                 "(BLE allows one connection), move the Mac closer, and power-cycle the lamp.")
    return dev


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def print_state(name, state):
    if state:
        print(f"Connected to {name}: power {'on' if state['on'] else 'off'}, brightness {state['brightness']}%, "
              f"RGB {tuple(state['rgb'])}")
    else:
        print(f"Connected to {name}, write characteristic present (the lamp did not answer the state request).")


async def cmd_scan(cfg, args):
    rows = await scan(cfg, args.all)
    if not rows:
        print("No lamps found. Close the vendor app, power-cycle the lamp, move closer, and retry "
              "(or use --all to list every BLE device).")
        return 1
    for i, (name, address, rssi, _d) in enumerate(rows, 1):
        print(f"{i:2}. {name:20} signal {rssi:4} dBm   {address}")
    return 0


async def cmd_setup(cfg, args):
    print("Scanning for lamps (close the vendor phone app first)...")
    rows = await scan(cfg, args.all)
    if args.name:
        rows = [r for r in rows if r[0] == args.name]
    if not rows:
        print("No matching lamp found. Close the vendor app, power-cycle the lamp, move closer, and retry.")
        return 1
    print("Lamps nearby. Neighbours' lamps can show up too; the strongest signal is usually yours.")
    for i, (name, address, rssi, _d) in enumerate(rows, 1):
        print(f"{i:2}. {name:20} signal {rssi:4} dBm")
    remaining = list(rows)
    while remaining:
        if len(remaining) == 1 and args.name:
            choice = remaining[0]
        else:
            pick = ask(f"Which number is your lamp? [1-{len(rows)}, q to quit] ")
            if pick.lower() in ("q", ""):
                return 1
            if not pick.isdigit() or not 1 <= int(pick) <= len(rows):
                continue
            choice = rows[int(pick) - 1]
        name, address, _rssi, device = choice
        print(f"Connecting to {name} and blinking it...")
        try:
            async with Lamp(cfg, device) as lamp:
                print_state(name, await lamp.read_state())
                await lamp.blink()
        except Exception as exc:  # noqa: BLE001 - report any BLE failure to the user and let them retry
            print(f"Could not control {name}: {exc}")
            if choice in remaining:
                remaining.remove(choice)
            continue
        if args.yes or ask("Did YOUR lamp just blink? [y/N] ").lower().startswith("y"):
            save_lamp(args.config, name, address)
            print(f"Saved {name} to {args.config}. The Mac can control your lamp.\n"
                  "Next: start the bridge (cd bridge && node lamp.mjs) and pair it with Alexa.")
            return 0
        if choice in remaining:
            remaining.remove(choice)
        print("Not that one. Pick another.")
    return 1


async def cmd_check(cfg, args):
    dev = await find_configured(cfg)
    async with Lamp(cfg, dev) as lamp:
        state = await lamp.read_state()
    print_state(cfg["ble_name"] or cfg["ble_address"], state)
    return 0


async def cmd_control(cfg, frames):
    dev = await find_configured(cfg)
    async with Lamp(cfg, dev) as lamp:
        await lamp.write(frames)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print control frames as hex, do not connect")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="path to config.json")
    ap.add_argument("--all", action="store_true", help="scan/setup: list every named BLE device, not only lamps")
    ap.add_argument("--name", help="setup: only consider the lamp with this BLE name")
    ap.add_argument("--yes", action="store_true", help="setup: save without asking whether the lamp blinked")
    ap.add_argument("command", choices=["scan", "setup", "check", "on", "off", "rgb", "bright"])
    ap.add_argument("values", nargs="*")
    args = ap.parse_args(argv)

    frames = None
    if args.command in ("on", "off", "rgb", "bright"):
        try:
            frames = frames_for(args.command, args.values)
        except ValueError as exc:
            ap.error(str(exc))
        if args.dry_run:
            for f in frames:
                print(f.hex(" "))
            return 0

    cfg = load_config(args.config)
    if args.command == "scan":
        return asyncio.run(cmd_scan(cfg, args))

    lock_path = os.path.expanduser(cfg["lock_path"])
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    # One BLE connection at a time: concurrent callers queue on this lock instead of racing for the radio.
    with open(lock_path, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        if args.command == "setup":
            return asyncio.run(cmd_setup(cfg, args))
        if args.command == "check":
            return asyncio.run(cmd_check(cfg, args))
        return asyncio.run(cmd_control(cfg, frames))


if __name__ == "__main__":
    sys.exit(main())
