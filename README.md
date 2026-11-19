# BLE Lamp → Matter → Alexa

Control a Bluetooth-only LED lamp with Alexa by making your Mac pretend to be a Matter light.

```
"Alexa, turn on bedroom light"
   Echo  ──Matter over Wi-Fi──▶  Mac (this bridge)  ──Bluetooth LE──▶  lamp
```

Alexa only adds Wi-Fi/Matter/Zigbee lights. Cheap BLE lamps that only work with a phone app are refused
("only Wi-Fi devices are supported"). This project runs a small [matter.js](https://github.com/project-chip/matter.js)
node on your Mac that advertises itself as a Matter **Extended Color Light**. Alexa pairs with it like any
other Matter bulb. Every on/off, brightness and colour change Alexa sends is translated into BLE writes to
the lamp.

What works: on/off, brightness, colour (hue/saturation), colour temperature ("warm white", "cool white"),
routines, the Alexa app slider and colour picker.

Tested with: a DayBetter LED strip (BLE name like `P0xx_xxxx`), an Echo Pop as the Matter controller,
macOS with Node 23 and Python 3 + [bleak](https://github.com/hbldh/bleak), matter.js 0.18. Other Matter
controllers (Google Home, Apple Home, Home Assistant) should work too but have not been tested.

## Contents

| Path | What it is |
|---|---|
| `bridge/lamp.mjs` | The Matter node. Exposes one colour light, turns Matter attribute changes into `lampctl.py` calls. |
| `lamp/lampctl.py` | Finds and verifies your lamp (`scan`, `setup`, `check`) and controls it (`on`, `off`, `rgb R G B`, `bright PCT`). Has `--dry-run`. |
| `config.example.json` | Template; `lampctl.py setup` copies it to `config.json` and fills in your lamp. |
| `macos/start-lamp-bridge.command` | Runs the bridge in Terminal with auto-restart and `caffeinate`. |
| `macos/com.example.ble-lamp-bridge.plist.example` | Optional LaunchAgent to start the bridge at login. |
| `tests/test_lampctl.py` | CRC and frame tests (no Bluetooth needed). |

## Requirements

- A Mac with Bluetooth, on the same Wi-Fi network as your Echo, close enough to reach the lamp over BLE.
- Node.js 20 or newer.
- Python 3.9 or newer.
- An Echo that acts as a Matter controller (most recent Echo models, including Echo Pop and Echo Dot 4/5).
- A lamp that speaks the protocol below (see [Lamp protocol](#lamp-protocol)). Other lamps need a different `lampctl.py`.

## Setup

### 1. Install

```sh
git clone <this repo> ble-lamp-matter-bridge
cd ble-lamp-matter-bridge

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cd bridge && npm install && cd ..
```

### 2. Connect the Mac to YOUR lamp (do this first)

Every lamp has its own Bluetooth name (DayBetter strips look like `P065_xxxx`, with a different suffix per
lamp). Neighbours' lamps of the same model often show up in the same scan, and signal strength does not
reliably tell you which one is yours. The setup command finds the lamps, blinks the one you pick, and only
saves it after you confirm that your lamp blinked.

Fully close the vendor phone app first. BLE lamps accept one connection at a time, and the app holds it.

```sh
.venv/bin/python lamp/lampctl.py setup
```

```
Scanning for lamps (close the vendor phone app first)...
 1. P065_c3d4            signal  -63 dBm
 2. P065_a1b2            signal  -75 dBm
Which number is your lamp? [1-2, q to quit] 2
Connecting to P065_a1b2 and blinking it...
Connected to P065_a1b2: power on, brightness 10%, RGB (25, 2, 0)
Did YOUR lamp just blink? [y/N] y
Saved P065_a1b2 to config.json. The Mac can control your lamp.
```

If you answer "n", pick another number. Setup creates `config.json` from `config.example.json` and stores the
lamp's name and its macOS Bluetooth identifier. The first Bluetooth call may trigger a macOS permission
prompt for your terminal app. Allow it.

Other useful commands:

```sh
.venv/bin/python lamp/lampctl.py scan     # list lamps only (add --all to list every BLE device)
.venv/bin/python lamp/lampctl.py check    # connect to the saved lamp, print its state, exit 0 if it works
```

Lamp not in the list: close the vendor app, power-cycle the lamp, move the Mac closer, and run setup again.

### 3. Configure the bridge

Open `config.json` and set `bridge.python` to the Python that has `bleak`, for example the absolute path to
`.venv/bin/python`. Everything else has working defaults.

| Key | Meaning |
|---|---|
| `lamp.ble_name` / `lamp.ble_address` | Written by `setup`. The address is the macOS-specific identifier (not a MAC address). |
| `lamp.service_uuid` / `lamp.name_pattern` | How `scan` recognises lamps. Defaults match DayBetter strips. |
| `lamp.write_uuid` / `lamp.notify_uuid` | GATT characteristics. The defaults are right for DayBetter strips. |
| `lamp.lock_path` | Lock file that serialises BLE access between processes. |
| `bridge.python` | Python that has `bleak` installed. |
| `bridge.device_name` | Name Alexa shows by default (you can rename it in the Alexa app). |
| `bridge.node_id` | matter.js storage name (`~/.matter/<node_id>`). Changing it means pairing again. |
| `bridge.port` | Matter UDP port, default 5540. |
| `bridge.snap_orange_to_rgb` | Optional. Orange-ish hues from Alexa are replaced with this exact RGB value. Set `null` to disable. |

### 4. Try the lamp directly (optional)

```sh
.venv/bin/python lamp/lampctl.py --dry-run rgb 255 0 0   # prints frames, no Bluetooth
.venv/bin/python lamp/lampctl.py on
.venv/bin/python lamp/lampctl.py rgb 0 0 255
.venv/bin/python lamp/lampctl.py bright 50
.venv/bin/python lamp/lampctl.py off
```

### 5. Start the bridge (Matter)

```sh
cd bridge && node lamp.mjs
```

The bridge checks the lamp before anything else. Matter starts only after the Mac has connected to the lamp
and read its state:

```
LAMP OK: Connected to P065_a1b2: power on, brightness 10%, RGB (25, 2, 0)
READY: not paired.
Manual pairing code: 1234-567-8901
QR payload: MT:XXXXXXXXXXXXXXXXXXX
```

If the lamp is not reachable, the bridge prints `LAMP NOT REACHABLE` and retries every 10 seconds. It
doesn't advertise to Alexa until the lamp check passes. If no lamp is configured, it exits and tells you to
run `setup`.

On first start, the bridge also creates `.pairing.json` with a random Matter passcode and discriminator
for your install. That file is kept out of git.

The first time, macOS asks whether your terminal may find devices on the local network. Allow it, or
Alexa will never discover the bridge.

### 6. Pair with Alexa

1. Alexa app → **Devices** → **+** → **Add Device** → **Matter** (or "Other" → Matter).
2. When asked for a code, either type the **manual pairing code**, or turn the `MT:...` payload into a QR
   code (for example with the [CHIP QR code tool](https://project-chip.github.io/connectedhomeip/qrcode.html)) and scan it.
3. Alexa warns that the device is not Matter certified (it uses the Matter test vendor ID). Accept.
4. Put it in a room and name it. "Alexa, turn on bedroom light."

The pairing is stored by matter.js in `~/.matter/<node_id>` and survives bridge restarts.
After pairing, the bridge prints `READY: already paired`.

### 7. Keep it running (optional)

`macos/start-lamp-bridge.command` runs the bridge in a loop, restarts it if it crashes, and keeps the Mac
from idle-sleeping. Double-click it, or start it at login:

1. Copy `macos/com.example.ble-lamp-bridge.plist.example` to `~/Library/LaunchAgents/com.example.ble-lamp-bridge.plist`.
2. Replace `/PATH/TO` with the absolute path of your checkout.
3. `launchctl load ~/Library/LaunchAgents/com.example.ble-lamp-bridge.plist`

If `node` comes from nvm, set `NODE_BIN_DIR` at the top of the `.command` script. Logs go to
`~/Library/Logs/ble-lamp-matter-bridge.log`.

**Why go through Terminal?** macOS grants Local Network (mDNS/multicast) and Bluetooth permission
per app. A process started directly by launchd gets neither permission, so Matter discovery fails
without any visible error. The LaunchAgent opens the script in Terminal, so the bridge uses the
permissions you already granted to Terminal. This start-at-login path has not yet been tested across a full reboot.

## How it works

- `lamp.mjs` creates a matter.js `ServerNode` with one `ExtendedColorLightDevice` endpoint (on/off, level
  control, colour control with hue/saturation, XY and colour temperature).
- It listens for attribute changes and calls `lampctl.py`:
  - `onOff` → `on` / `off`
  - `currentLevel` (0–254) → `bright` 1–100 %
  - `currentHue` / `currentSaturation` → RGB (HSV with V = 1)
  - `colorTemperatureMireds` → RGB (Tanner Helland approximation)
- Calls run one at a time, and rapid updates are coalesced: if Alexa sends 20 brightness values while
  you drag the slider, only the newest one waiting in the queue is sent.

matter.js 0.18 details that cost time:

- The colour control cluster needs `remainingTime` marked mandatory (`.alter({ attributes: { remainingTime: { optional: false } } })`).
- The colour control state needs explicit initial values (`colorMode`, `colorCapabilities`, mired limits,
  `coupleColorTempToLevelMinMireds`, `startUpColorTemperatureMireds`, …), otherwise startup fails validation.

## Lamp protocol

Tested on DayBetter BLE LED strips. Other brands differ.

**Frame:** `A0 <cmd> <len> <payload…> <crc_lo> <crc_hi>`

- `len` = payload length + 3.
- CRC is CRC-16/MODBUS (init `0xFFFF`, reflected poly `0xA001`, no final XOR) over every byte before it, little-endian.
- Write to characteristic `0000a065-0000-1000-8000-00805f9b34fb`. Notifications and replies arrive on `0000f065-0000-1000-8000-00805f9b34fb`.

| cmd | payload | meaning |
|---|---|---|
| `0x11` | `01` / `00` | power on / off |
| `0x12` | `01 00` | static colour mode |
| `0x13` | 1 byte, 1–100 | brightness percent (values below 10 are clamped to 10) |
| `0x15` | `R G B FF` | colour |
| `0x10` | `01` | read state; reply `A1 10 …` = on, mode, sub-mode, brightness, speed, sensitivity, R, G, B, … (RGB is reported pre-multiplied by brightness) |

Example: power on = `a0 11 04 01 b1 21`. Run `lamp/lampctl.py --dry-run on` to see this frame.

**Rules that matter** (brightness silently does nothing if you skip them):

1. Subscribe to the notify characteristic before writing.
2. Send the mode frame (`0x12 01 00`) before brightness (`0x13`) or colour (`0x15`).
3. Write every frame **with response**. Write-without-response drops the brightness command.
4. Brightness 0 does nothing useful. Use the power-off command.
5. Only one BLE central at a time: close the vendor app. If the lamp disappears from scans, power-cycle it.

## Limitations

- The Mac has to stay awake and within Bluetooth range of the lamp. If the Mac sleeps or the lid is closed, the lamp stops responding.
- Alexa has no concept of vendor "scenes" or modes. For something like a dim orange "rest mode", create an
  Alexa Routine that sets the colour and brightness 1 %. The lamp clamps that to 10 %.
- Exposing extra modes as separate switches would need a Matter Aggregator (bridged devices). That
  changes the endpoint layout and requires pairing again. It is not implemented.
- The device uses the Matter test vendor ID `0xFFF1`. That is fine for personal use, but it is not certified.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Alexa cannot find the device while pairing | Allow Local Network access for your terminal (System Settings → Privacy & Security → Local Network). Make sure the Mac and Echo are on the same network and VLAN. |
| `… not found` from `lampctl.py`, or `LAMP NOT REACHABLE` from the bridge | Close the vendor app completely, move closer, power-cycle the lamp. |
| On/off and colour work, but brightness does not change | Make sure you are using this `lampctl.py`, which follows the rules above. |
| Pairing code changed | `.pairing.json` was deleted. Restore it, or remove the device in Alexa and pair again. |
| Need to start over | Remove the device in the Alexa app, delete `~/.matter/<node_id>` and `.pairing.json`, then restart. |

## Tests

```sh
python3 -m unittest discover -s tests
node --check bridge/lamp.mjs
```

The tests check the CRC against the standard MODBUS check value (`CRC16("123456789") = 0x4B37`) and the
frame layout and order. They do not need Bluetooth or bleak.

## Credits and legal

- The lamp protocol was reverse engineered from the vendor's own app, only to make the author's own device
  interoperable. This repo contains no vendor binaries, decompiled code, or app assets.
- Protocol corrections (length byte, mode and colour payloads) were confirmed against
  [ma67-ex/ClaudeLamp](https://github.com/ma67-ex/ClaudeLamp) (MIT). The code here is an independent implementation, not a copy.
- Matter stack: [matter.js](https://github.com/project-chip/matter.js) (Apache-2.0).
- Not affiliated with Amazon, DayBetter, or the Connectivity Standards Alliance. "Alexa" and "Matter" are trademarks of their owners.

## License

MIT. See [LICENSE](LICENSE).
