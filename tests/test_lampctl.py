import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lamp"))

from lampctl import DEFAULTS, crc16_modbus, frame, frames_for, is_candidate, parse_state  # noqa: E402


class StateTest(unittest.TestCase):
    def test_parse_reply(self):
        # A1 10 len | on mode sub brightness speed sens R G B
        reply = bytes([0xA1, 0x10, 0x0C, 1, 1, 0, 10, 0, 0, 25, 2, 0, 0, 0])
        self.assertEqual(parse_state(reply), {"on": True, "mode": 1, "brightness": 10, "rgb": [25, 2, 0]})

    def test_rejects_other_frames(self):
        self.assertIsNone(parse_state(frame(0x11, b"\x01")))
        self.assertIsNone(parse_state(b"\xa1\x10"))


class CandidateTest(unittest.TestCase):
    def test_service_uuid_or_name(self):
        self.assertTrue(is_candidate(None, [DEFAULTS["service_uuid"].upper()], DEFAULTS))
        self.assertTrue(is_candidate("P065_a1b2", [], DEFAULTS))
        self.assertFalse(is_candidate("Someone's AirPods", [], DEFAULTS))


class CrcTest(unittest.TestCase):
    def test_modbus_check_value(self):
        self.assertEqual(crc16_modbus(b"123456789"), 0x4B37)

    def test_empty(self):
        self.assertEqual(crc16_modbus(b""), 0xFFFF)


class FrameTest(unittest.TestCase):
    def test_layout(self):
        f = frame(0x11, b"\x01")
        self.assertEqual(f[:4], bytes([0xA0, 0x11, 0x04, 0x01]))
        self.assertEqual(len(f), 6)
        self.assertEqual(int.from_bytes(f[-2:], "little"), crc16_modbus(f[:-2]))

    def test_brightness_sends_mode_first(self):
        cmds = [f[1] for f in frames_for("bright", ["50"])]
        self.assertEqual(cmds, [0x11, 0x12, 0x13])

    def test_values_are_clamped(self):
        self.assertEqual(frames_for("bright", ["500"])[-1][3], 100)
        self.assertEqual(frames_for("rgb", ["300", "-5", "10"])[-1][3:7], bytes([255, 0, 10, 0xFF]))

    def test_bad_input(self):
        with self.assertRaises(ValueError):
            frames_for("rgb", ["1"])
        with self.assertRaises(ValueError):
            frames_for("blink", [])


if __name__ == "__main__":
    unittest.main()
