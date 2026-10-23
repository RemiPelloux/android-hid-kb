# hidkb — rooted Android phone as a USB HID keyboard

Turn an Android phone into a boot-protocol **USB keyboard** through configfs and
the kernel's `f_hid` function, then type any text file into whatever host the
phone is plugged into.

No gadgetfs, no `g_hid` module, no compiler on the device, no reboot, no
Bluetooth. Works from a plain `adb shell` as root.

```
   ┌──────────────┐   USB-C    ┌──────────────────────────────┐
   │  host        │◄──────────►│ phone (root)                 │
   │ console / PC │            │  configfs gadget → f_hid     │
   │ /dev/input   │            │  /dev/hidg0 ← mksh builtins  │
   └──────────────┘            └──────────────────────────────┘
        types into the focused window at 50 ms/keystroke, ±1 ms
```

Developed and verified against the ephemeral root from
**[GalaxyCrash](https://github.com/RemiPelloux/GalaxyCrash)** — *root on a locked
bootloader, no custom kernel, no Odin, no data wipe* (Samsung, Android 12–17,
`arm64-v8a`, `u:r:ksu:s0`). That root is what makes this work on a phone you
cannot flash:

```bash
adb devices    # phone visible
adb shell su -c id
# uid=0(root) context=u:r:ksu:s0
```

---

## Quick start

```bash
cd tools/android-hid-kb

./hidkb.py doctor                        # can this phone be a USB device at all?
./hidkb.py setup                         # create the gadget (port untouched, ADB stays up)
./hidkb.py selftest --verify             # bind, type "hello", restore — watch keycodes locally
./hidkb.py send examples/punctuation.txt # type a real file
./hidkb.py teardown                      # remove the gadget, give the port back
```

`selftest --verify` on a Linux host must print, in order:
`LEFTSHIFT down, H down, LEFTSHIFT up, H up, …` — that is the proof the gadget
speaks HID correctly.

If you are on Wi-Fi ADB (`adb tcpip 5555 && adb connect <ip>:5555`) the port is
free for the target device while you keep full control. With USB ADB the phone
must be unplugged from the PC and plugged into the target.

## Requirements

| Requirement | Why | Check |
|---|---|---|
| Root shell (`su -c`) | configfs writes + `mknod` | `hidkb.py doctor` |
| A USB **device** controller | phones with host-only ports cannot do this | `/sys/class/udc/*` non-empty |
| `CONFIG_USB_CONFIGFS_F_HID` | the HID function itself | `/sys/class/hidg` appears once bound |
| configfs mounted at `/config` | gadget tree | `ls /config/usb_gadget` |
| host side | any USB host | a PC, a console, a BIOS |

Nothing else: no `python`, `perl`, `busybox`, NDK or cross-compiler on the phone.
Everything on-device is mksh builtins.

## Commands

| Command | What it does |
|---|---|
| `doctor` | read-only probe: root, UDC, configfs, `f_hid`, `mknod`, which gadget owns the port, which vendor HAL will fight you |
| `setup` | creates `/config/usb_gadget/hidkb` + `hid_f` function; **does not touch the port**, so ADB stays up |
| `send FILE` | binds, types the file, restores |
| `selftest` | binds, types `hello`, restores; `--verify` also decodes keycodes on this host |
| `teardown` | unbinds, removes the gadget, re-binds the stock one |
| `verify` | runs `pc/watch_input.py` to decode this host's `/dev/input` |

### `send` options

```
--profile plain|smilebasic   smilebasic = inverted letter case + inherited indentation
--case-invert / --no-case-invert
--indent literal|inherit     inherit = send only the *delta* of leading spaces after Enter
--hold MS                    key-down time (default 10)
--gap MS                     between keystrokes (default 40)
--prewait S / --postwait S   extra settle time around the bind
--backspaces N               send N Backspaces first (clears a dirty editor line)
--watchdog S                 forced restore if something hangs (default 1800)
--progress N                 emit a progress line every N keystrokes
--dry-run                    generate the on-device script, send nothing
--out PATH                   keep the generated script
--gadget DIR / --udc NAME    non-default paths
```

Estimate is printed before anything is sent: `keystrokes × (hold + gap)`.

## Worked example: 7 620 keystrokes into a Nintendo Switch 2

The reason this exists: the ns2jb exploit requires a 224-line BASIC program in
SmileBASIC 4.4.9, and typing it by hand on a console is miserable.

```bash
./hidkb.py doctor                                    # confirm UDC + f_hid
./hidkb.py setup
./hidkb.py send bootstrap.bas --profile smilebasic --hold 10 --gap 40
#   → 7620 keystrokes, ~6.5 min
```

* `--profile smilebasic` sends **Shift for lowercase** letters, because
  SmileBASIC's USB path maps an unshifted key to UPPERCASE.
* `--indent inherit` reproduces the editor's habit of carrying the previous
  line's leading spaces into the new line, so indentation is not doubled.
* Verified end to end: indentation test, full 224-line bootstrap, and the
  follow-up `LOAD`/`RUN`/`REMOVE`/`V=START(...)` commands, all typed by the
  phone, zero manual keystrokes.

Verify a transfer before using it: `examples/indent-test.bas` must arrive with
leading-space counts `0,1,1,2,1,1,0,0`.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `couldn't find an available UDC or it's busy` | vendor USB HAL re-bound its gadget | do not add any delay between releasing the old gadget and claiming the UDC; `hidkb-lib.sh` already races it |
| `hidg0 never appeared` | kernel has no `f_hid`, or bind never happened | `doctor`; try `su -c 'modprobe usb_f_hid'` |
| `mknod failed` | not root, or no `/sys/class/hidg/hidg0/dev` | check `id -u`; node is only created after a successful bind |
| host enumerates but nothing is typed | wrong modifier/key slot order, wrong protocol, or the host's text field is not focused | run `selftest --verify` on a Linux host and decode the keycodes |
| characters permuted (`a`↔`q`, `z`↔`w`) | host layout is AZERTY/QWERTZ | positions are US-QWERTY by design; add a layout map if needed |
| typed text is uppercase where it should be lowercase | the target inverts case (SmileBASIC) | `--profile smilebasic` or `--case-invert` |
| indentation doubles each line | target editor inherits indentation | `--indent inherit` |
| ADB/MTP gone after a run | gadget still bound | `hidkb.py teardown`; the trap/watchdog normally does it already |
| phone charges from the host, nothing enumerates | USB role reversed | unplug, replug, or use a data-capable cable |

## Safety

* `setup` never touches the port.
* `send` wraps the bind in `trap 'hidkb_restore; exit 0' INT TERM EXIT`, ignores
  `SIGHUP` (so a Wi-Fi ADB hiccup does not abort mid-transcript), and arms a
  watchdog subshell. The stock gadget is recorded in
  `/data/local/tmp/hidkb-owner` and re-bound automatically.
* Ctrl-C during `send` restores the port from the host side.
* The worst realistic outcome is "ADB is gone until you unplug/replug" — no
  persistent state is written outside `/data/local/tmp` and configfs.

## Files

```
hidkb.py                  host CLI: doctor / setup / send / selftest / teardown / verify
android/hidkb-lib.sh      on-device helpers (mksh): race bind, mknod, restore, teardown
pc/watch_input.py         Linux-side verifier: decodes /dev/input/event* keycodes
examples/                 hello.txt, indent-test.bas, punctuation.txt
docs/implementation.md    why each trick exists, with the dmesg evidence
```

Deep dive: [`docs/implementation.md`](docs/implementation.md) — the UDC race, the
missing udev node, the fork-cost measurements, the report format, and the
verification that caught a swapped-byte bug.

## License

MIT. See [`LICENSE`](LICENSE).

Not affiliated with Samsung, Google, Nintendo or the GalaxyCrash project. Use it
on hardware you own or are authorised to test.
