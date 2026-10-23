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

KEY_ENTER = 0x28
KEY_ESC = 0x29
KEY_BACKSPACE = 0x2A
KEY_TAB = 0x2B
KEY_SPACE = 0x2C

PUNCT = {
    "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31, ";": 0x33,
    "'": 0x34, "`": 0x35, ",": 0x36, ".": 0x37, "/": 0x38,
}
PUNCT_SHIFTED = {
    "_": 0x2D, "+": 0x2E, "{": 0x2F, "}": 0x30, "|": 0x31, ":": 0x33,
    '"': 0x34, "~": 0x35, "<": 0x36, ">": 0x37, "?": 0x38,
}
DIGIT_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5",
    "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
}


def usage_for_digit(ch: str) -> int:
    """USB HID usage for '1'..'9','0' -> 0x1E..0x26,0x27."""
    return 0x27 if ch == "0" else 0x1E + (ord(ch) - ord("1"))


def encode(ch: str, case_invert: bool = False):
    """Map one character to (modifiers, hid usage) or None if untypable."""
    if "a" <= ch <= "z" or "A" <= ch <= "Z":
        upper = ch.isupper()
        if case_invert:
            upper = not upper
        return (MOD_LSHIFT if upper else 0), 0x04 + (ord(ch.lower()) - ord("a"))
    if "0" <= ch <= "9":
        return 0, usage_for_digit(ch)
    if ch in DIGIT_SHIFTED:
        return MOD_LSHIFT, usage_for_digit(DIGIT_SHIFTED[ch])
    if ch in PUNCT:
        return 0, PUNCT[ch]
    if ch in PUNCT_SHIFTED:
        return MOD_LSHIFT, PUNCT_SHIFTED[ch]
    if ch == "\n":
        return 0, KEY_ENTER
    if ch == "\t":
        return 0, KEY_TAB
    if ch == " ":
        return 0, KEY_SPACE
    return None


# --------------------------------------------------------------------------- #
# keystroke planning
# --------------------------------------------------------------------------- #
def build_keystrokes(lines, indent: str = "literal", case_invert: bool = False):
    """Expand text lines into (mods, usage) key events.

    indent == "inherit" reproduces the behaviour of line editors that carry the
    previous line's leading spaces into the new line (SmileBASIC's editor does):
    after Enter we emit only the *difference* — extra spaces, or Backspaces when
    the next line is shallower. Never Home/Shift+Home, and never keyword-based
    guessing.
    """
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
            enc = encode(ch, case_invert)
            if enc is None:
                raise SystemExit(
                    f"{getattr(lines, 'name', '<input>')}:{lineno}:{col}: "
                    f"cannot type {ch!r} (0x{ord(ch):02x})"
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
                  gadget: str, udc: str) -> str:
    hold = f"{hold_ms / 1000:.3f}"
    gap = f"{gap_ms / 1000:.3f}"
    out = []
    w = out.append
    w("#!/system/bin/sh")
    w("# generated by hidkb.py — keystroke transcript, do not edit by hand")
    w(f"# source={source} keystrokes={len(keys)} hold={hold_ms}ms gap={gap_ms}ms")
    w(f". {REMOTE_LIB}")
    w(f"HIDKB_GADGET={gadget}")
    w(f"HIDKB_UDC={udc or ''}")
    w(f"HIDKB_LOG={REMOTE_LOG}")
    w('exec >>"$HIDKB_LOG" 2>&1')
    w('echo "=== hidkb run: $(date) ==="')
    w(f'echo "source={source} keystrokes={len(keys)} hold={hold_ms}ms gap={gap_ms}ms"')
    # HUP is ignored on purpose: if the Wi-Fi ADB link hiccups mid-transcript the
    # typing must continue. INT/TERM/EXIT always hands the port back.
    w('trap "" HUP')
    w("trap 'hidkb_restore; exit 0' INT TERM EXIT")
    w(f'( read -t {watchdog} _wd < /dev/zero; hidkb_msg "watchdog fired"; hidkb_restore ) &')
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
    add("hidg class (f_hid present)", info.get("hidg_class") == "yes",
        info.get("f_hid_config", "") or "CONFIG_USB_CONFIGFS_F_HID not found in config")
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
    if info.get("hidg_class") != "yes":
        print("  The kernel has no f_hid. If CONFIG_USB_CONFIGFS_F_HID=m, try:"
              "\n    su -c 'modprobe usb_f_hid'\n")
    if not udcs:
        print("  No UDC means this phone cannot act as a USB device at all"
              " (some devices need a USB-C OTG/role switch).\n")
    return 0


def cmd_setup(args):
    serial = detect_serial(args.serial)
    ensure_remote_env(serial, args.gadget)
    res = su_shell(serial, f". {REMOTE_LIB}\nHIDKB_GADGET={args.gadget}\nhidkb_setup_gadget\n",
                   timeout=120)
    sys.stdout.write(res.stdout or "")
    sys.stderr.write(res.stderr or "")
    return 0 if res.returncode == 0 else 1


def cmd_teardown(args):
    serial = detect_serial(args.serial)
    ensure_remote_env(serial, args.gadget)
    res = su_shell(serial, f". {REMOTE_LIB}\nHIDKB_GADGET={args.gadget}\nhidkb_teardown\n", timeout=120)
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
    keys = build_keystrokes(text_lines, indent=indent, case_invert=case_invert)
    script = render_script(
        keys, source=source, hold_ms=args.hold, gap_ms=args.gap, prewait=args.prewait,
        postwait=args.postwait, backspaces=args.backspaces, watchdog=args.watchdog,
        progress=args.progress, gadget=args.gadget, udc=args.udc,
    )
    return keys, script, case_invert, indent


def do_send(args, text_lines, source):
    keys, script, case_invert, indent = plan(args, text_lines, source)
    seconds = len(keys) * (args.hold + args.gap) / 1000
    print(f"  {source}: {len(text_lines)} lines, {len(keys)} keystrokes, "
          f"hold {args.hold} ms, gap {args.gap} ms, indent={indent}, "
          f"case_invert={case_invert}")
    print(f"  estimated duration: {seconds / 60:.1f} min")

    if args.dry_run:
        dest = Path(args.out) if args.out else Path(tempfile.gettempdir()) / "hidkb-dryrun.sh"
        dest.write_text(script)
        print(f"  dry run — script written to {dest} ({len(script)} bytes), nothing sent")
        return 0

    serial = detect_serial(args.serial)
    ensure_remote_env(serial, args.gadget)
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as fh:
        fh.write(script)
        path = fh.name
    try:
        push(serial, path, REMOTE_RUN)
    finally:
        os.unlink(path)
    if args.out:
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
def add_send_args(p, *, standalone=True):
    p.add_argument("file", nargs="?" if not standalone else None, help="text file to type")
    p.add_argument("--profile", choices=["plain", "smilebasic"], default="plain",
                   help="smilebasic = inverted letter case + inherited indentation")
    p.add_argument("--case-invert", dest="case_invert", action="store_true", default=None,
                   help="swap Shift on letters (SmileBASIC maps unshifted to UPPERCASE)")
    p.add_argument("--no-case-invert", dest="case_invert", action="store_false")
    p.add_argument("--indent", choices=["literal", "inherit"], default=None,
                   help="inherit = only send the delta of leading spaces after Enter")
    p.add_argument("--hold", type=int, default=10, help="ms a key stays down (default 10)")
    p.add_argument("--gap", type=int, default=40, help="ms between keystrokes (default 40)")
    p.add_argument("--prewait", type=float, default=0.5, help="s to wait after binding")
    p.add_argument("--postwait", type=float, default=0.0, help="s to stay bound after typing")
    p.add_argument("--backspaces", type=int, default=0, help="send N Backspaces first")
    p.add_argument("--watchdog", type=int, default=1800, help="s before a forced restore")
    p.add_argument("--progress", type=int, default=500, help="emit a progress line every N keys")
    p.add_argument("--dry-run", action="store_true", help="generate the script, send nothing")
    p.add_argument("--out", help="keep the generated on-device script at this path")
    p.add_argument("--gadget", default=DEFAULT_GADGET, help="configfs gadget directory")
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

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
