// Matter bridge for a Bluetooth-only lamp:
//   Alexa (or any Matter controller) -> Matter over Wi-Fi -> this Node process -> lampctl.py -> BLE -> lamp.
// Exposes one Extended Color Light endpoint: on/off, brightness, hue/saturation, colour temperature.
import "@matter/nodejs";
import { ServerNode, VendorId } from "@matter/main";
import { ExtendedColorLightDevice } from "@matter/main/devices";
import { ColorControlServer } from "@matter/main/behaviors";
import { execFile } from "node:child_process";
import { randomInt } from "node:crypto";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..");
const CONFIG_PATH = process.env.LAMP_CONFIG ?? join(ROOT, "config.json");
const PAIRING_PATH = join(ROOT, ".pairing.json");
const LAMPCTL = join(ROOT, "lamp", "lampctl.py");

if (!existsSync(CONFIG_PATH)) {
    console.error(`Missing ${CONFIG_PATH}. Copy config.example.json to config.json and set lamp.ble_name.`);
    process.exit(1);
}
const config = JSON.parse(readFileSync(CONFIG_PATH, "utf8"));
const bridge = {
    python: "python3",
    device_name: "Bedroom Light",
    node_id: "ble-lamp-matter-bridge",
    port: 5540,
    snap_orange_to_rgb: null,
    ...config.bridge,
};

// Per-install pairing secrets: generated once, stored next to the code, never committed (.gitignore).
// Regenerating on every start would change the pairing code, so they are persisted.
const FORBIDDEN_PASSCODES = [0, 11111111, 22222222, 33333333, 44444444, 55555555, 66666666, 77777777,
    88888888, 99999999, 12345678, 87654321];
function loadOrCreatePairing() {
    if (existsSync(PAIRING_PATH)) return JSON.parse(readFileSync(PAIRING_PATH, "utf8"));
    let passcode;
    do passcode = randomInt(1, 99999999); while (FORBIDDEN_PASSCODES.includes(passcode));
    const pairing = { passcode, discriminator: randomInt(0, 4096) };
    writeFileSync(PAIRING_PATH, JSON.stringify(pairing, null, 2) + "\n", { mode: 0o600 });
    return pairing;
}
const pairing = loadOrCreatePairing();

// Step 1: prove the Mac can reach and control the lamp over Bluetooth. Matter (and therefore Alexa)
// only starts after this succeeds, so you never pair Alexa with a bridge that cannot drive the lamp.
if (!config.lamp?.ble_name && !config.lamp?.ble_address) {
    console.error("No lamp configured. Run first:  python3 lamp/lampctl.py setup");
    process.exit(1);
}
function checkLamp() {
    return new Promise(resolve => {
        execFile(bridge.python, [LAMPCTL, "--config", CONFIG_PATH, "check"], { timeout: 60000 }, (err, out, errOut) => {
            resolve({ ok: !err, message: (err ? errOut || err.message : out).trim() });
        });
    });
}
for (let attempt = 1; ; attempt++) {
    const { ok, message } = await checkLamp();
    if (ok) {
        console.log(`LAMP OK: ${message}`);
        break;
    }
    console.error(`LAMP NOT REACHABLE (attempt ${attempt}): ${message}\nRetrying in 10 s...`);
    await new Promise(r => setTimeout(r, 10000));
}

// Serialise BLE calls (one connection at a time) and coalesce rapid updates: only the newest
// value per key is sent, so dragging a slider in the Alexa app does not queue dozens of writes.
let busy = false;
const pending = new Map();
function send(key, args) {
    pending.set(key, args);
    pump();
}
function pump() {
    if (busy || pending.size === 0) return;
    const [key, args] = pending.entries().next().value;
    pending.delete(key);
    busy = true;
    execFile(bridge.python, [LAMPCTL, "--config", CONFIG_PATH, ...args], { timeout: 60000 }, (err, _out, errOut) => {
        if (err) console.error("lampctl failed:", args.join(" "), (errOut || err.message).trim());
        else console.log("lampctl ok:", args.join(" "));
        busy = false;
        pump();
    });
}

// Matter hue/saturation are 0-254. Optionally snap orange-ish hues to one exact RGB value
// (useful when a controller's "orange" does not match the colour you want from the lamp).
function hsToRgb(h254, s254) {
    const h = (h254 / 254) * 360, s = s254 / 254;
    if (bridge.snap_orange_to_rgb && h >= 5 && h <= 40 && s >= 0.8) return bridge.snap_orange_to_rgb;
    const c = s, x = c * (1 - Math.abs(((h / 60) % 2) - 1)), m = 1 - c;
    const [r, g, b] = h < 60 ? [c, x, 0] : h < 120 ? [x, c, 0] : h < 180 ? [0, c, x]
        : h < 240 ? [0, x, c] : h < 300 ? [x, 0, c] : [c, 0, x];
    return [r, g, b].map(n => Math.round((n + m) * 255));
}
// Colour temperature (mireds) to RGB, Tanner Helland's approximation.
function miredsToRgb(mireds) {
    const k = 1_000_000 / mireds / 100;
    const r = k <= 66 ? 255 : 329.698727446 * Math.pow(k - 60, -0.1332047592);
    const g = k <= 66 ? 99.4708025861 * Math.log(k) - 161.1195681661 : 288.1221695283 * Math.pow(k - 60, -0.0755148492);
    const b = k >= 66 ? 255 : k <= 19 ? 0 : 138.5177312231 * Math.log(k - 10) - 305.0447927307;
    return [r, g, b].map(n => Math.max(0, Math.min(255, Math.round(n))));
}

const node = await ServerNode.create({
    id: bridge.node_id, // also the storage folder name under ~/.matter; changing it means re-pairing
    network: { port: bridge.port },
    commissioning: pairing,
    productDescription: { name: bridge.device_name, deviceType: ExtendedColorLightDevice.deviceType },
    basicInformation: {
        vendorName: "DIY",
        vendorId: VendorId(0xfff1), // Matter test vendor id; controllers warn the device is not certified
        nodeLabel: bridge.device_name,
        productName: "BLE Lamp Matter Bridge",
        productLabel: bridge.device_name,
        productId: 0x8000,
        serialNumber: `bridge-${pairing.discriminator}`,
        uniqueId: `bridge-${pairing.discriminator}`,
    },
});

// remainingTime must be mandatory and the colour state below must be set explicitly,
// otherwise matter.js 0.18 throws a validation error at startup.
const LampDevice = ExtendedColorLightDevice.with(
    ColorControlServer.with("HueSaturation", "Xy", "ColorTemperature")
        .alter({ attributes: { remainingTime: { optional: false } } }),
);
const light = await node.add(LampDevice, {
    id: "light",
    number: 1,
    colorControl: {
        colorMode: 0,
        enhancedColorMode: 0,
        colorCapabilities: { hueSaturation: true, enhancedHue: true, colorLoop: false, xy: true, colorTemperature: true },
        colorTempPhysicalMinMireds: 153,
        colorTempPhysicalMaxMireds: 500,
        coupleColorTempToLevelMinMireds: 153,
        startUpColorTemperatureMireds: null,
        colorTemperatureMireds: 250,
        currentHue: 8,
        currentSaturation: 254,
    },
});

light.events.onOff.onOff$Changed.on(on => send("power", on ? ["on"] : ["off"]));
light.events.levelControl.currentLevel$Changed.on(level => {
    if (level == null) return;
    // Matter level 0-254 -> lamp percent 1-100 (the lamp itself clamps below 10).
    send("bright", ["bright", String(Math.max(1, Math.round((level / 254) * 100)))]);
});
const colorUpdate = () => {
    const cc = light.state.colorControl;
    const rgb = cc.colorMode === 2 && cc.colorTemperatureMireds
        ? miredsToRgb(cc.colorTemperatureMireds)
        : hsToRgb(cc.currentHue ?? 0, cc.currentSaturation ?? 254);
    send("color", ["rgb", ...rgb.map(String)]);
};
light.events.colorControl.currentHue$Changed.on(colorUpdate);
light.events.colorControl.currentSaturation$Changed.on(colorUpdate);
light.events.colorControl.colorTemperatureMireds$Changed.on(colorUpdate);

await node.start();
if (node.lifecycle.isCommissioned) {
    console.log("READY: already paired");
} else {
    const { qrPairingCode, manualPairingCode } = node.state.commissioning.pairingCodes;
    console.log(`READY: not paired.\nManual pairing code: ${manualPairingCode}\nQR payload: ${qrPairingCode}`);
}
