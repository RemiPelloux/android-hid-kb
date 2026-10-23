#!/usr/bin/env python3
"""Watch a Linux host's /dev/input for a newly attached keyboard and decode it.

Proves that the emulated gadget really speaks HID: run this, then bind the
gadget on the phone. The script waits for a new event node to appear, then
prints every EV_KEY it receives in Linux keycode space, which is what the host
kernel decoded from the 8-byte boot reports.

    python3 pc/watch_input.py --seconds 30

Needs read access to /dev/input/event* — usually the `input` group.
"""
from __future__ import annotations

import argparse
import glob
import os
import struct
import sys
import time

# struct input_event on 64-bit: struct timeval (2x long) + type + code + value
EVENT = "llHHi"
SIZE = struct.calcsize(EVENT)
EV_KEY = 0x01

# Linux keycodes we expect to see from a US-QWERTY keyboard.
NAMES = {
    1: "ESC", 2: "1", 3: "2", 4: "3", 5: "4", 6: "5", 7: "6", 8: "7", 9: "8",
    10: "9", 11: "0", 12: "-", 13: "=", 14: "BACKSPACE", 15: "TAB", 16: "Q",
    17: "W", 18: "E", 19: "R", 20: "T", 21: "Y", 22: "U", 23: "I", 24: "O",
    25: "P", 28: "ENTER", 29: "LEFTCTRL", 30: "A", 31: "S", 32: "D", 33: "F",
    34: "G", 35: "H", 36: "J", 37: "K", 38: "L", 39: ";", 40: "'", 42: "LEFTSHIFT",
    43: "\\", 44: "Z", 45: "X", 46: "C", 47: "V", 48: "B", 49: "N", 50: "M",
    54: "RIGHTSHIFT", 56: "LEFTALT", 57: "SPACE", 96: "RIGHTCTRL", 100: "RIGHTALT",
    125: "LEFTMETA", 126: "RIGHTMETA",
}


def nodes() -> set[str]:
    return set(glob.glob("/dev/input/event*"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seconds", type=int, default=60, help="how long to keep reading")
    ap.add_argument("--wait", type=int, default=120, help="how long to wait for a new device")
    args = ap.parse_args()

    baseline = nodes()
    print(f"watching for a new input device ({len(baseline)} present)…", flush=True)

    new, deadline = None, time.time() + args.wait
    while time.time() < deadline:
        fresh = nodes() - baseline
        if fresh:
            new = sorted(fresh)[0]
            break
        time.sleep(0.1)
    if not new:
        print("no new input device appeared — the host never enumerated a keyboard")
        return 1
    print(f"new device: {new}\n", flush=True)

    count = 0
    end = time.time() + args.seconds
    try:
        with open(new, "rb", buffering=0) as fh:
            os.set_blocking(fh.fileno(), False)
            while time.time() < end:
                try:
                    data = fh.read(SIZE * 64)
                except (BlockingIOError, InterruptedError):
                    time.sleep(0.005)
                    continue
                if not data:
                    time.sleep(0.005)
                    continue
                for off in range(0, len(data) - SIZE + 1, SIZE):
                    _, _, kind, code, value = struct.unpack(EVENT, data[off:off + SIZE])
                    if kind != EV_KEY:
                        continue
                    count += 1
                    label = NAMES.get(code, f"code{code}")
                    state = {0: "up", 1: "down", 2: "repeat"}.get(value, str(value))
                    print(f"  {label:12} {state}", flush=True)
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"device went away: {exc}")
    print(f"\n{count} key events decoded")
    return 0 if count else 1


if __name__ == "__main__":
    sys.exit(main())
