#!/usr/bin/env python3
"""hidkb — use a rooted Android phone as a USB HID keyboard.

The phone's USB device controller is turned into a boot-protocol HID keyboard
through configfs and the kernel's `f_hid` function, so no gadgetfs, no
`g_hid` module, no compiler and no reboot are needed. It then types text into
whatever host it is plugged into — a game console, another PC, a BIOS prompt,
an installer, a locked kiosk.

Everything runs on the phone in pure mksh with **zero forks**, which buys ~1 ms
keystroke accuracy and makes a 7 600-key transcript (≈6.5 min) reliable.

Commands
    doctor    check every prerequisite on the phone, read-only
    setup     create the configfs gadget (does not touch the USB port)
    send      type a file into the focused window of the host
    selftest  bind, type "hello", restore — the 5-second smoke test
    teardown  unbind, destroy the gadget, hand the port back to ADB/MTP
    verify    watch /dev/input on a Linux host and print received keycodes
    calibrate detect the host's keyboard layout from what it actually types

Run `hidkb.py <command> -h` for the full option list.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PC_DIR = HERE / "pc"

REMOTE_DIR = "/data/local/tmp"
REMOTE_LIB = f"{REMOTE_DIR}/hidkb-lib.sh"
REMOTE_DESC = f"{REMOTE_DIR}/hidkb-report_desc.bin"
REMOTE_RUN = f"{REMOTE_DIR}/hidkb-run.sh"
REMOTE_LOG = f"{REMOTE_DIR}/hidkb.log"
REMOTE_OWNER = f"{REMOTE_DIR}/hidkb-owner"

DEFAULT_GADGET = "/config/usb_gadget/hidkb"

# --------------------------------------------------------------------------- #
# HID report descriptor — canonical 63-byte boot keyboard, 8-byte reports.
#
#  05 01        Usage Page (Generic Desktop)
#  09 06        Usage (Keyboard)
#  A1 01        Collection (Application)
#  05 07          Usage Page (Keyboard/Keypad)
#  19 E0 29 E7    Usage Minimum 0xE0, Maximum 0xE7   (8 modifier keys)
#  15 00 25 01    Logical Minimum 0, Maximum 1
#  75 01 95 08    Report Size 1 bit x 8
#  81 02          Input (Data, Variable, Absolute)   -> byte 0: modifier bitmap
#  95 01 75 08    Report Size 8 bits x 1
#  81 03          Input (Constant)                   -> byte 1: reserved
#  95 05 75 01    Report Size 1 bit x 5
#  05 08          Usage Page (LEDs)
#  19 01 29 05    Usage Minimum 1, Maximum 5
#  91 02          Output (Data, Variable, Absolute)  -> LED state (host -> device)
#  95 01 75 03    Report Size 3 bits x 1             (LED byte padding)
#  91 03          Output (Constant)
#  95 06 75 08    Report Size 8 bits x 6
#  15 00 25 65    Logical Minimum 0, Maximum 0x65
#  05 07          Usage Page (Keyboard/Keypad)
#  19 00 29 65    Usage Minimum 0, Maximum 0x65
#  81 00          Input (Data, Array)                -> bytes 2..7: up to 6 keys
#  C0           End Collection
# --------------------------------------------------------------------------- #
DESCRIPTOR = bytes([
    0x05, 0x01, 0x09, 0x06, 0xA1, 0x01,
    0x05, 0x07, 0x19, 0xE0, 0x29, 0xE7,
    0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x08, 0x81, 0x02,
    0x95, 0x01, 0x75, 0x08, 0x81, 0x03,
    0x95, 0x05, 0x75, 0x01, 0x05, 0x08, 0x19, 0x01, 0x29, 0x05, 0x91, 0x02,
    0x95, 0x01, 0x75, 0x03, 0x91, 0x03,
    0x95, 0x06, 0x75, 0x08, 0x15, 0x00, 0x25, 0x65,
    0x05, 0x07, 0x19, 0x00, 0x29, 0x65, 0x81, 0x00,
    0xC0,
])

# Modifier bits (byte 0 of the report).
MOD_LCTRL, MOD_LSHIFT, MOD_LALT, MOD_LGUI = 0x01, 0x02, 0x04, 0x08
MOD_RALT = 0x40   # Right Alt = AltGr, needed for AZERTY's third level (# @ { } [ ] \ | …)

KEY_ENTER = 0x28
KEY_ESC = 0x29
KEY_BACKSPACE = 0x2A
KEY_TAB = 0x2B
KEY_SPACE = 0x2C

# --------------------------------------------------------------------------- #
# Host keyboard layouts
#
# HID carries key *positions* (usages), never characters. What glyph a position
# produces is decided by the HOST's active layout. The tool therefore has to
# know the host layout to pick the position that yields a wanted character.
#
# Each layout is a list of rows: (usage, plain, shifted, altgr). `plain` is the
# glyph with no modifier, `shifted` with Shift, `altgr` with AltGr (Right Alt).
# Use None where a level produces no ASCII glyph (e.g. AZERTY's accented keys).
# We invert these rows into glyph -> (modifiers, usage).
#
#   us  US-QWERTY (the original behaviour; a US host maps positions 1:1).
#   fr  French AZERTY (ISO). Verified against on-screen probes on a Switch 2
#       whose SmileBASIC keyboard is French: a->Q, q->A, z->W, w->Z, etc.
# --------------------------------------------------------------------------- #
US_ROWS = [
    (0x04, "a", "A", None), (0x05, "b", "B", None), (0x06, "c", "C", None),
    (0x07, "d", "D", None), (0x08, "e", "E", None), (0x09, "f", "F", None),
    (0x0A, "g", "G", None), (0x0B, "h", "H", None), (0x0C, "i", "I", None),
    (0x0D, "j", "J", None), (0x0E, "k", "K", None), (0x0F, "l", "L", None),
    (0x10, "m", "M", None), (0x11, "n", "N", None), (0x12, "o", "O", None),
    (0x13, "p", "P", None), (0x14, "q", "Q", None), (0x15, "r", "R", None),
    (0x16, "s", "S", None), (0x17, "t", "T", None), (0x18, "u", "U", None),
    (0x19, "v", "V", None), (0x1A, "w", "W", None), (0x1B, "x", "X", None),
    (0x1C, "y", "Y", None), (0x1D, "z", "Z", None),
    (0x1E, "1", "!", None), (0x1F, "2", "@", None), (0x20, "3", "#", None),
    (0x21, "4", "$", None), (0x22, "5", "%", None), (0x23, "6", "^", None),
    (0x24, "7", "&", None), (0x25, "8", "*", None), (0x26, "9", "(", None),
    (0x27, "0", ")", None),
    (0x2D, "-", "_", None), (0x2E, "=", "+", None), (0x2F, "[", "{", None),
    (0x30, "]", "}", None), (0x31, "\\", "|", None), (0x33, ";", ":", None),
    (0x34, "'", '"', None), (0x35, "`", "~", None), (0x36, ",", "<", None),
    (0x37, ".", ">", None), (0x38, "/", "?", None),
]

# French AZERTY (ISO). plain/shifted/altgr; accented levels left as None.
FR_ROWS = [
    # top letter row
    (0x14, "a", "A", None), (0x1A, "z", "Z", None), (0x08, "e", "E", None),
    (0x15, "r", "R", None), (0x17, "t", "T", None), (0x1C, "y", "Y", None),
    (0x18, "u", "U", None), (0x0C, "i", "I", None), (0x12, "o", "O", None),
    (0x13, "p", "P", None),
    # home row
    (0x04, "q", "Q", None), (0x16, "s", "S", None), (0x07, "d", "D", None),
    (0x09, "f", "F", None), (0x0A, "g", "G", None), (0x0B, "h", "H", None),
    (0x0D, "j", "J", None), (0x0E, "k", "K", None), (0x0F, "l", "L", None),
    (0x33, "m", "M", None),
    # bottom row
    (0x1D, "w", "W", None), (0x1B, "x", "X", None), (0x06, "c", "C", None),
    (0x19, "v", "V", None), (0x05, "b", "B", None), (0x11, "n", "N", None),
    (0x10, ",", "?", None),            # US-m position = AZERTY comma/question
    # number row: plain = symbol, Shift = the digit, AltGr = programming symbol
    (0x1E, "&", "1", None), (0x1F, None, "2", "~"), (0x20, '"', "3", "#"),
    (0x21, "'", "4", "{"), (0x22, "(", "5", "["), (0x23, "-", "6", "|"),
    (0x24, None, "7", "`"), (0x25, "_", "8", "\\"), (0x26, None, "9", "^"),
    (0x27, None, "0", "@"),
    # right-hand punctuation
    (0x2D, ")", None, "]"), (0x2E, "=", "+", "}"), (0x30, "$", None, None),
    (0x31, "*", None, None), (0x34, None, "%", None), (0x36, ";", ".", None),
    (0x37, ":", "/", None), (0x38, "!", None, None),
    # the ISO extra key (left of the home row's far right); AZERTY < >
    (0x64, "<", ">", None),
]


class Layout:
    """A host keyboard layout: inverts position->glyph rows into glyph->keys."""

    def __init__(self, name, rows):
        self.name = name
        self.rev = {}    # glyph -> (modifiers, usage); first row wins on duplicates
        self.plain = {}  # usage -> unshifted glyph (for calibration/detection)
        for usage, plain, shifted, altgr in rows:
            if plain is not None:
                self.rev.setdefault(plain, (0, usage))
                self.plain[usage] = plain
            if shifted is not None:
                self.rev.setdefault(shifted, (MOD_LSHIFT, usage))
            if altgr is not None:
                self.rev.setdefault(altgr, (MOD_RALT, usage))

    def encode(self, ch: str, shift_invert: bool = False):
        """Map one character to (modifiers, hid usage) or None if untypable.

        shift_invert compensates for a target that renders a key as if the Shift
        state were the opposite of what was pressed. SmileBASIC does this, and
        (verified on a Switch 2) it does it for the first two levels but NOT for
        AltGr: an unshifted number-row key gives the digit, a shifted one gives
        the symbol — reversed from a PC — while AltGr keys (# @ { } …) behave
        normally. So we flip the Shift bit on plain/shifted glyph keys and leave
        AltGr keys exactly as the layout gives them (AltGr+Shift is dropped by the
        console). Enter, Tab and Space are left untouched too, since Shift+Enter
        can mean something else to an editor.
        """
        if ch == "\n":
            return (0, KEY_ENTER)
        if ch == "\t":
            return (0, KEY_TAB)
        if ch == " ":
            return (0, KEY_SPACE)
        enc = self.rev.get(ch)
        if enc is None:
            return None
        mods, usage = enc
        if shift_invert and not (mods & MOD_RALT):
            mods ^= MOD_LSHIFT
        return (mods, usage)


LAYOUTS = {"us": Layout("us", US_ROWS), "fr": Layout("fr", FR_ROWS)}

# `calibrate` writes the detected layout name here; `--layout auto` reads it, so a
# machine only has to be calibrated against its host once.
DETECT_FILE = HERE / ".hidkb-layout"


def save_detected_layout(name: str) -> None:
    try:
        DETECT_FILE.write_text(name.strip() + "\n")
    except OSError:
        pass


def resolve_layout(name: str) -> "Layout":
    """Turn a --layout value into a Layout; 'auto' reads the calibrated default."""
    if name == "auto":
        try:
            name = DETECT_FILE.read_text().strip()
        except OSError:
            name = "us"
        name = name if name in LAYOUTS else "us"
    return LAYOUTS[name]


# --------------------------------------------------------------------------- #
# keystroke planning
# --------------------------------------------------------------------------- #
def build_keystrokes(lines, indent: str = "literal", case_invert: bool = False,
                     layout: "Layout | None" = None):
    """Expand text lines into (mods, usage) key events.

    indent == "inherit" reproduces the behaviour of line editors that carry the
    previous line's leading spaces into the new line (SmileBASIC's editor does):
    after Enter we emit only the *difference* — extra spaces, or Backspaces when
    the next line is shallower. Never Home/Shift+Home, and never keyword-based
    guessing.
    """
    layout = layout or LAYOUTS["us"]
    carried = 0
    out = []
    for lineno, raw in enumerate(lines, 1):
        line = raw.rstrip("\r\n")
        body = line.strip()
        if indent == "inherit":
            want = len(line) - len(line.lstrip(" "))
            delta = want - carried
            out.extend([(0, KEY_SPACE)] * delta if delta > 0 else [(0, KEY_BACKSPACE)] * -delta)
            carried = want
            text = body
        else:
            text = line
        for col, ch in enumerate(text, 1):
            enc = layout.encode(ch, case_invert)
            if enc is None:
                raise SystemExit(
                    f"{getattr(lines, 'name', '<input>')}:{lineno}:{col}: "
                    f"cannot type {ch!r} (0x{ord(ch):02x}) on the {layout.name!r} layout"
                )
            out.append(enc)
        out.append((0, KEY_ENTER))
    return out


def oct_escape(value: int) -> str:
    """mksh `print` understands \\0NNN (octal); it has no \\xNN."""
    return "\\0%03o" % value


def report(m: int, key: int) -> str:
    """8-byte boot report: modifiers, reserved, key1..key6."""
    return "".join(oct_escape(b) for b in (m, 0, key, 0, 0, 0, 0, 0))


def render_script(keys, *, source: str, hold_ms: int, gap_ms: int, prewait: float,
                  postwait: float, backspaces: int, watchdog: int, progress: int,
                  gadget: str, udc: str, mode: str = "auto") -> str:
    hold = f"{hold_ms / 1000:.3f}"
    gap = f"{gap_ms / 1000:.3f}"
    out = []
    w = out.append
    w("#!/system/bin/sh")
    w("# generated by hidkb.py — keystroke transcript, do not edit by hand")
    w(f"# source={source} keystrokes={len(keys)} hold={hold_ms}ms gap={gap_ms}ms")
    w(f". {REMOTE_LIB}")
    w(f"HIDKB_GADGET={gadget}")
    w(f"HIDKB_MODE={mode}")
    w(f"HIDKB_UDC={udc or ''}")
    w(f"HIDKB_LOG={REMOTE_LOG}")
    w('exec >>"$HIDKB_LOG" 2>&1')
    w('echo "=== hidkb run: $(date) ==="')
    w(f'echo "source={source} keystrokes={len(keys)} hold={hold_ms}ms gap={gap_ms}ms"')
    # HUP is ignored on purpose: if the Wi-Fi ADB link hiccups mid-transcript the
    # typing must continue. INT/TERM/EXIT always hands the port back.
    w('trap "" HUP')
    w("trap 'hidkb_restore; exit 0' INT TERM EXIT")
    # Set up the idle (non-spinning) sleep in the parent shell first, so the fifo
    # fd is open before the watchdog subshell is forked and inherits it. Both the
    # main loop and the watchdog then sleep with ~0 CPU instead of pinning a core.
    w("hidkb_nap_init")
    w(f'( hidkb_nap {watchdog}; hidkb_msg "watchdog fired"; hidkb_restore ) &')
    w("WDPID=$!")
    w("hidkb_bind || exit 1")
    w(f"hidkb_nap {prewait}")
    w("exec 4>/dev/hidg0")
    w('hidkb_msg "typing"')
    for n in range(backspaces):
        w(f"print -n '{report(0, KEY_BACKSPACE)}' >&4")
        w(f"hidkb_nap {hold}")
        w(f"print -n '{report(0, 0)}' >&4")
        w(f"hidkb_nap {gap}")
    for i, (mods, key) in enumerate(keys, 1):
        w(f"print -n '{report(mods, key)}' >&4")
        w(f"hidkb_nap {hold}")
        w(f"print -n '{report(0, 0)}' >&4")
        if i != len(keys):
            w(f"hidkb_nap {gap}")
        if progress and i % progress == 0 and i != len(keys):
            w(f'echo "progress {i}/{len(keys)}"')
    w("exec 4>&-")
    w(f'hidkb_msg "typing done ({len(keys)} keystrokes)"')
    if postwait:
        w(f"hidkb_nap {postwait}")
    w("kill $WDPID 2>/dev/null")
    w("trap - INT TERM EXIT   # the explicit restore below is the last one")
    w("hidkb_restore")
    return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- #
# adb plumbing
# --------------------------------------------------------------------------- #
class AdbError(RuntimeError):
    pass


def adb_base(serial):
    return ["adb"] + (["-s", serial] if serial else [])


def adb(serial, args, *, timeout=None, capture=True, check=False):
    cmd = adb_base(serial) + args
    res = subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout)
    if check and res.returncode != 0:
        raise AdbError(f"{shlex.join(cmd)} failed: {(res.stdout or '') + (res.stderr or '')}")
    return res


def detect_serial(explicit):
    out = subprocess.run(["adb", "devices"], capture_output=True, text=True).stdout
    ready = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            ready.append(parts[0])
    if explicit:
        if explicit not in ready:
            raise SystemExit(f"device {explicit!r} not in `adb devices` ({ready or 'none'})")
        return explicit
    if not ready:
        raise SystemExit("no adb device. Plug the phone in, or `adb connect <ip>:5555`.")
    if len(ready) > 1:
        raise SystemExit("multiple adb devices, choose one with -s:\n  " + "\n  ".join(ready))
    return ready[0]


def push(serial, local, remote):
    adb(serial, ["push", str(local), remote], check=True, timeout=120)


def su_shell(serial, body, *, timeout=120, capture=True):
    """Push a tiny script and run it as root (KernelSU/Magisk `su -c`)."""
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(body)
        path = fh.name
    try:
        push(serial, path, REMOTE_RUN)
    finally:
        os.unlink(path)
    return adb(serial, ["shell", f"su -c 'sh {REMOTE_RUN}'"], timeout=timeout, capture=capture)


def ensure_remote_env(serial, gadget=DEFAULT_GADGET):
    """Push the device-side library and the report descriptor."""
    push(serial, HERE / "android" / "hidkb-lib.sh", REMOTE_LIB)
    with tempfile.NamedTemporaryFile("wb", suffix=".bin", delete=False) as fh:
        fh.write(DESCRIPTOR)
        path = fh.name
    try:
        push(serial, path, REMOTE_DESC)
    finally:
        os.unlink(path)
    return gadget


def read_log(serial, offset):
    """Return (new_text, new_offset) for the device-side log."""
    res = adb(serial, ["shell", f"su -c 'cat {REMOTE_LOG}'"], timeout=60)
    data = res.stdout or ""
    return data[offset:], len(data)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
PROBE = r"""
echo "model=$(getprop ro.product.model)"
echo "release=$(getprop ro.build.version.release)"
echo "kernel=$(uname -r)"
echo "uid=$(id -u)"
echo "selinux=$(cat /proc/self/attr/current 2>/dev/null | tr -d '\000')"
echo "configfs=$([ -d /config/usb_gadget ] && echo yes || echo no)"
echo "udcs=$(for u in /sys/class/udc/*; do [ -e "$u" ] && printf '%s ' "${u##*/}"; done)"
echo "gadgets=$(for g in /config/usb_gadget/*/; do [ -d "$g" ] && [ "${g}" != "/config/usb_gadget/*/" ] && printf '%s ' "${g%/}"; done)"
echo "owners=$(for g in /config/usb_gadget/*/; do v=$(cat "$g/UDC" 2>/dev/null); [ -n "$v" ] && printf '%s=%s ' "${g%/}" "$v"; done)"
echo "hidg_class=$([ -e /sys/class/hidg ] && echo yes || echo no)"
echo "hidg_dev=$([ -e /dev/hidg0 ] && echo yes || echo no)"
echo "hidg_majmin=$(cat /sys/class/hidg/hidg0/dev 2>/dev/null)"
echo "f_hid_module=$([ -d /sys/module/usb_f_hid ] && echo module || echo no)"
CONF=$( (zcat /proc/config.gz 2>/dev/null || cat /boot/config-$(uname -r) 2>/dev/null) | grep -E '^CONFIG_USB_CONFIGFS_F_HID' | head -1)
echo "f_hid_config=${CONF:-unknown}"
echo "usb_services=$(getprop | grep -oE 'init\.svc\.[a-z0-9_.-]*usb[a-z0-9_.-]*: \[[a-z]*\]' | tr '\n' ' ')"
echo "usb_hal=$(ps -A -o NAME 2>/dev/null | grep -E 'android.hardware.usb-service' | head -1)"
MK=$(mknod /data/local/tmp/.hidkb-mknod c 1 3 2>&1 && echo ok && rm -f /data/local/tmp/.hidkb-mknod)
echo "mknod=${MK:-failed}"
echo "android_devs=$(for d in /sys/devices/virtual/android_usb/android*; do [ -e "$d" ] && printf '%s ' "${d##*/}"; done)"
"""


def cmd_doctor(args):
    serial = detect_serial(args.serial)
    res = su_shell(serial, PROBE, timeout=90)
    raw = (res.stdout or "") + (res.stderr or "")
    info = {}
    for line in raw.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            info[k.strip()] = v.strip()

    ok, warn, bad = "\033[32mOK\033[0m", "\033[33mWARN\033[0m", "\033[31mFAIL\033[0m"
    rows = []

    def add(check, good, detail="", warn_only=False):
        status = ok if good else (warn if warn_only else bad)
        rows.append((status, check, detail))

    add("adb device", True, serial)
    add("root shell", info.get("uid") == "0", f"uid={info.get('uid')} selinux={info.get('selinux')}")
    add("configfs mounted", info.get("configfs") == "yes", "/config/usb_gadget")
    udcs = info.get("udcs", "").split()
    add("USB device controller", bool(udcs), info.get("udcs", "") or "host-only port, no gadget mode")
    # /sys/class/hidg only exists while a HID function is bound (it disappears
    # with the last hidg device), so judge f_hid by the kernel config instead.
    fhid = info.get("f_hid_config", "")
    fhid_ok = fhid.endswith("=y") or fhid.endswith("=m")
    add("f_hid in kernel", fhid_ok,
        (fhid or "CONFIG_USB_CONFIGFS_F_HID not found")
        + ("  (hidg class appears when a function binds)" if not info.get("hidg_class") == "yes" else "  (hidg class live)"))
    add("mknod available", info.get("mknod") == "ok", "needed: Android's ueventd has no hidg rule")
    owner = info.get("owners", "").strip()
    add("UDC owned by", True, owner or "nothing")
    add("vendor USB HAL", bool(info.get("usb_hal")),
        (info.get("usb_hal") or "none") + "  (will fight for the UDC; we race it)", warn_only=True)
    add("/dev/hidg0 node", info.get("hidg_dev") == "yes",
        "created by this tool via mknod" if info.get("hidg_dev") != "yes" else "present", warn_only=True)
    add("gadget dir", True, f"{args.gadget} " + ("exists" if args.gadget in info.get("gadgets", "") else "not created yet"))

    print(f"\n  hidkb doctor — {info.get('model', '?')} / Android {info.get('release', '?')} / {info.get('kernel', '?')}\n")
    for status, check, detail in rows:
        print(f"  [{status}] {check:28} {detail}")
    print()
    if not (fhid.endswith("=y") or fhid.endswith("=m")):
        print("  The kernel has no f_hid. If CONFIG_USB_CONFIGFS_F_HID=m, try:"
              "\n    su -c 'modprobe usb_f_hid'\n")
    leaked = info.get("android_devs", "").count(" ")
    if leaked >= 3:
        print(f"  {leaked} leftover android_usb platform devices: creating new gadget"
              " directories will fail.\n  Use --mode vendor (default auto does this for you).\n")
    if not udcs:
        print("  No UDC means this phone cannot act as a USB device at all"
              " (some devices need a USB-C OTG/role switch).\n")
    return 0


def cmd_setup(args):
    serial = detect_serial(args.serial)
    ensure_remote_env(serial, args.gadget)
    res = su_shell(serial, f". {REMOTE_LIB}\nHIDKB_GADGET={args.gadget}\n"
                           f"HIDKB_MODE={getattr(args, 'mode', 'auto')}\nhidkb_setup_gadget\n",
                   timeout=120)
    sys.stdout.write(res.stdout or "")
    sys.stderr.write(res.stderr or "")
    return 0 if res.returncode == 0 else 1


def cmd_teardown(args):
    serial = detect_serial(args.serial)
    ensure_remote_env(serial, args.gadget)
    res = su_shell(serial, f". {REMOTE_LIB}\nHIDKB_GADGET={args.gadget}\n"
                           f"HIDKB_MODE={getattr(args, 'mode', 'auto')}\nhidkb_teardown\n", timeout=120)
    sys.stdout.write(res.stdout or "")
    sys.stderr.write(res.stderr or "")
    return 0 if res.returncode == 0 else 1


def plan(args, text_lines, source):
    if args.profile == "smilebasic":
        case_invert = True if args.case_invert is None else args.case_invert
        indent = "inherit" if args.indent is None else args.indent
    else:
        case_invert = bool(args.case_invert)
        indent = args.indent or "literal"
    layout = resolve_layout(getattr(args, "layout", "us") or "us")
    keys = build_keystrokes(text_lines, indent=indent, case_invert=case_invert,
                            layout=layout)
    script = render_script(
        keys, source=source, hold_ms=args.hold, gap_ms=args.gap, prewait=args.prewait,
        postwait=args.postwait, backspaces=args.backspaces, watchdog=args.watchdog,
        progress=args.progress, gadget=args.gadget, udc=args.udc,
        mode=getattr(args, "mode", "auto"),
    )
    return keys, script, case_invert, indent


def run_on_device(args, script, seconds):
    """Push the generated script, run it as root, stream its log, restore on Ctrl-C.

    Returns the remote process's exit code. Shared by `send`, `selftest` and
    `calibrate` so they all get the same bind/stream/restore behaviour.
    """
    serial = detect_serial(args.serial)
    ensure_remote_env(serial, args.gadget)
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(script)
        path = fh.name
    try:
        push(serial, path, REMOTE_RUN)
    finally:
        os.unlink(path)
    if getattr(args, "out", None):
        Path(args.out).write_text(script)

    adb(serial, ["shell", f"su -c 'rm -f {REMOTE_LOG} {REMOTE_OWNER}'"])

    print("  binding gadget and typing — do not touch the target device\n")
    proc = subprocess.Popen(
        adb_base(serial) + ["shell", f"su -c 'sh {REMOTE_RUN}'"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    offset = 0
    deadline = time.time() + seconds * 3 + 180
    try:
        while True:
            time.sleep(1.5)
            try:
                chunk, offset = read_log(serial, offset)
            except Exception:
                chunk = ""
            for line in chunk.splitlines():
                if line.startswith("progress"):
                    done, total = line.split()[1].split("/")
                    pct = int(done) / max(int(total), 1) * 100
                    print(f"\r  typing {done}/{total} ({pct:5.1f}%)", end="", flush=True)
                elif line.strip():
                    print(f"\r  {line}" + " " * 40)
            if proc.poll() is not None:
                break
            if time.time() > deadline:
                print("\r  timeout — aborting" + " " * 40)
                proc.kill()
                break
    except KeyboardInterrupt:
        print("\n  interrupted — restoring the port")
        proc.kill()
        su_shell(serial, f". {REMOTE_LIB}\nHIDKB_GADGET={args.gadget}\nhidkb_restore\n", timeout=60)
        return 130

    chunk, _ = read_log(serial, offset)
    for line in chunk.splitlines():
        if line.startswith("progress"):
            continue
        if line.strip():
            print(f"  {line}")
    print()
    return proc.returncode


def do_send(args, text_lines, source):
    keys, script, case_invert, indent = plan(args, text_lines, source)
    seconds = len(keys) * (args.hold + args.gap) / 1000
    layout = resolve_layout(getattr(args, "layout", "us") or "us").name
    print(f"  {source}: {len(text_lines)} lines, {len(keys)} keystrokes, "
          f"hold {args.hold} ms, gap {args.gap} ms, indent={indent}, "
          f"case_invert={case_invert}, layout={layout}")
    print(f"  estimated duration: {seconds / 60:.1f} min")

    if args.dry_run:
        dest = Path(args.out) if args.out else Path(tempfile.gettempdir()) / "hidkb-dryrun.sh"
        dest.write_text(script)
        print(f"  dry run — script written to {dest} ({len(script)} bytes), nothing sent")
        return 0

    return run_on_device(args, script, seconds)


def cmd_send(args):
    path = Path(args.file)
    if not path.is_file():
        raise SystemExit(f"no such file: {path}")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    return do_send(args, lines, path.name)


def cmd_selftest(args):
    serial = detect_serial(args.serial)
    args.file = None
    watcher = None
    if args.verify:
        watcher = subprocess.Popen([sys.executable, str(PC_DIR / "watch_input.py")])
        time.sleep(1.0)
    args.prewait = args.prewait if args.prewait is not None else 8.0
    rc = do_send(args, ["hello\n"], "<selftest>")
    if watcher:
        try:
            watcher.wait(timeout=90)
        except subprocess.TimeoutExpired:
            watcher.kill()
    if rc == 0:
        print("  selftest sent. Expect 'hello' in the host's focused window.")
        print("  On the Switch: SmileBASIC editor gets h-e-l-l-o in lowercase.")
    return rc


def cmd_verify(args):
    script = PC_DIR / "watch_input.py"
    return subprocess.call([sys.executable, str(script), *(["--seconds", str(args.seconds)] if args.seconds else [])])


# --------------------------------------------------------------------------- #
# calibrate — detect the host's keyboard layout from what it actually renders
# --------------------------------------------------------------------------- #
# The host, not the phone, decides which glyph a key position produces, and it
# never tells us which layout it is using. So we send a fixed set of positions,
# have the operator read back the glyphs the host drew, and match those against
# every known layout. The letters row alone is enough to separate QWERTY from
# AZERTY and to reveal case-inversion (SmileBASIC draws the opposite case).
PROBE_LETTERS = list(range(0x04, 0x1E))   # usages 0x04..0x1D, US a..z order
PROBE_DIGITS = list(range(0x1E, 0x28))    # usages 0x1E..0x27, US 1..0 order


def _score(predicted: str, got: str) -> float:
    """Fraction of positions that match, over the length we were given."""
    got = got.strip()
    if not got:
        return 0.0
    n = min(len(predicted), len(got))
    if n == 0:
        return 0.0
    hits = sum(1 for a, b in zip(predicted[:n], got[:n]) if a == b)
    return hits / len(got)


def detect_layout(letters_seen: str):
    """Return (best_layout_name, case_invert, score) for the letters readback."""
    best = ("us", False, 0.0)
    for name, lay in LAYOUTS.items():
        predicted = "".join(lay.plain.get(u, "·") for u in PROBE_LETTERS)
        for ci in (False, True):
            cand = predicted.swapcase() if ci else predicted
            s = _score(cand, letters_seen)
            if s > best[2]:
                best = (name, ci, s)
    return best


def cmd_calibrate(args):
    # Build the probe directly as key positions (no layout applied — we are
    # measuring the host's layout, so we must send raw, unshifted positions).
    keys = []
    for u in PROBE_LETTERS:
        keys.append((0, u))
    keys.append((0, KEY_ENTER))
    for u in PROBE_DIGITS:
        keys.append((0, u))
    keys.append((0, KEY_ENTER))

    args.prewait = args.prewait if args.prewait is not None else 3.0
    script = render_script(
        keys, source="<calibrate>", hold_ms=args.hold, gap_ms=args.gap,
        prewait=args.prewait, postwait=args.postwait, backspaces=args.backspaces,
        watchdog=args.watchdog, progress=0, gadget=args.gadget, udc=args.udc,
        mode=getattr(args, "mode", "auto"),
    )
    seconds = len(keys) * (args.hold + args.gap) / 1000

    print("  calibration probe: 26 letter keys, then 10 number-row keys.")
    print("  Put the cursor in a text field on the host (SmileBASIC editor, a")
    print("  search box, anything that shows what you type), then watch the screen.\n")

    if args.dry_run:
        dest = Path(args.out) if args.out else Path(tempfile.gettempdir()) / "hidkb-calibrate.sh"
        dest.write_text(script)
        print(f"  dry run — probe script written to {dest}, nothing sent")
        return 0

    rc = run_on_device(args, script, seconds)
    if rc not in (0, None):
        print("  probe run did not finish cleanly; you can still enter what you saw.")

    print("\n  Now read the FIRST line off the host screen and type it here")
    print("  exactly — same characters, same case. Just press Enter to skip.")
    try:
        letters_seen = input("  line 1 (letters) > ")
    except (EOFError, KeyboardInterrupt):
        print("\n  calibration aborted.")
        return 1

    if not letters_seen.strip():
        print("\n  Nothing entered. If the host showed nothing at all, the keyboard")
        print("  never enumerated — try again with --mode vendor, or replug the cable.")
        return 1

    name, case_invert, score = detect_layout(letters_seen)
    pct = int(round(score * 100))
    print(f"\n  best match: layout '{name}', case-inversion {'on' if case_invert else 'off'} "
          f"({pct}% of the letters fit)")

    if score < 0.6:
        print("\n  Low confidence. Double-check you read line 1 left-to-right, or your")
        print("  host may use a layout this tool doesn't know yet (us, fr). You can add")
        print("  one as a *_ROWS table in hidkb.py and re-run calibrate.")
        return 1

    save_detected_layout(name)
    print(f"  saved '{name}' as the default for --layout auto (in {DETECT_FILE.name}).")
    profile = "--profile smilebasic" if case_invert else ""
    flags = f"--layout {name}" + (f" {profile}" if profile else "")
    print("\n  Recommended command for this host:")
    print(f"    ./hidkb.py send FILE {flags}".rstrip())
    if case_invert and name != "us":
        print("  (smilebasic profile also turns on inherited indentation; add")
        print("   --indent literal if your target does not inherit indent.)")
    return 0


# --------------------------------------------------------------------------- #
def add_send_args(p, *, standalone=True):
    p.add_argument("file", nargs="?" if not standalone else None, help="text file to type")
    p.add_argument("--profile", choices=["plain", "smilebasic"], default="plain",
                   help="smilebasic = inverted letter case + inherited indentation")
    p.add_argument("--case-invert", dest="case_invert", action="store_true", default=None,
                   help="invert Shift on every glyph key (SmileBASIC renders each key "
                        "with the opposite Shift state: letters, digits and symbols)")
    p.add_argument("--no-case-invert", dest="case_invert", action="store_false")
    p.add_argument("--indent", choices=["literal", "inherit"], default=None,
                   help="inherit = only send the delta of leading spaces after Enter")
    p.add_argument("--layout", choices=["auto", "us", "fr"], default="auto",
                   help="host keyboard layout the positions are decoded through "
                        "(auto = the one `calibrate` detected, else us)")
    p.add_argument("--hold", type=int, default=15, help="ms a key stays down (default 15)")
    p.add_argument("--gap", type=int, default=60, help="ms between keystrokes (default 60)")
    p.add_argument("--prewait", type=float, default=0.5, help="s to wait after binding")
    p.add_argument("--postwait", type=float, default=0.0, help="s to stay bound after typing")
    p.add_argument("--backspaces", type=int, default=0, help="send N Backspaces first")
    p.add_argument("--watchdog", type=int, default=1800, help="s before a forced restore")
    p.add_argument("--progress", type=int, default=500, help="emit a progress line every N keys")
    p.add_argument("--dry-run", action="store_true", help="generate the script, send nothing")
    p.add_argument("--out", help="keep the generated on-device script at this path")
    p.add_argument("--gadget", default=DEFAULT_GADGET, help="configfs gadget directory")
    p.add_argument("--mode", choices=["auto", "own", "vendor"], default="auto",
                   help="auto: use our own gadget if it can be created, else attach HID to "
                        "the vendor gadget (required on Samsung after a few gadget dirs)")
    p.add_argument("--udc", default="", help="UDC name (default: first in /sys/class/udc)")


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="hidkb.py", description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("-s", "--serial", help="adb serial (default: the only attached device)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="check prerequisites on the phone")
    d.add_argument("--gadget", default=DEFAULT_GADGET)
    d.set_defaults(func=cmd_doctor)

    for name, fn, helptext in (
        ("setup", cmd_setup, "create the configfs gadget (does not touch the port)"),
        ("teardown", cmd_teardown, "unbind, remove the gadget, restore ADB/MTP"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--gadget", default=DEFAULT_GADGET)
        p.add_argument("--mode", choices=["auto", "own", "vendor"], default="auto")
        p.set_defaults(func=fn)

    s = sub.add_parser("send", help="type a file into the host")
    add_send_args(s)
    s.set_defaults(func=cmd_send)

    st = sub.add_parser("selftest", help="type 'hello' and restore (smoke test)")
    add_send_args(st, standalone=False)
    st.add_argument("--verify", action="store_true", help="also watch /dev/input on this host")
    st.set_defaults(func=cmd_selftest, file=None)

    v = sub.add_parser("verify", help="watch this host's /dev/input for the keyboard")
    v.add_argument("--seconds", type=int, default=60)
    v.set_defaults(func=cmd_verify)

    c = sub.add_parser("calibrate", help="detect the host's keyboard layout from what it types")
    add_send_args(c, standalone=False)
    c.set_defaults(func=cmd_calibrate, file=None)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
