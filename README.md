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
        types into the focused window at 75 ms/keystroke, ±1 ms
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

## Why this exists: you cannot type the payload by hand

Everything else in this README is machinery for one sentence: **the file being
typed is bigger than a human can type correctly.**

The payload in question is ns2jb's `bootstrap.bas` — 224 lines, 7 804 bytes,
7 620 keystrokes once you count newlines, and it has to land in a SmileBASIC 4.4.9
editor on a locked handheld console. Why hand-typing is not an option:

| Problem | Reality |
|---|---|
| Volume | at a fast and error-free 4 keys/s that is **32 minutes** of solid typing |
| No paste | the console is a USB host, not a terminal. There is no clipboard, no `adb input text`, no file drop into the editor — the only channels are human hands or a USB keyboard |
| Scrolling | the editor shows ~15 lines. Line 200 is invisible while you type it, so typos are found by *running* the program, not by reading it |
| Auto-indent | the editor inherits the previous line's leading spaces. Type a line with the “right” spaces by hand and you get double indentation — a syntax error 40 lines later |
| Case mapping | SmileBASIC's USB path delivers unshifted letters as UPPERCASE, so a plain keyboard types `REM` where the payload needs `rem`-equivalent behaviour; you need Shift for every lowercase letter |
| Silent failure | this payload *is* a memory-corruption exploit: it discovers ASLR bases at runtime and prints a token. One wrong hex digit does not raise an error — it produces a wrong address, a hang, or a crash, and you cannot tell which line did it |
| Ephemerality | a usable exploit session is one run long. Retyping after a reboot is not 32 minutes of patience, it is 32 minutes *again*, every time |

The alternatives were all dead ends before this tool was written:

* **USB-C → USB-A adapter + a real keyboard** — not in the bag.
* **Raspberry Pi Pico / Flipper / Pi Zero as a HID** — same, and it is another
  device to buy, flash and carry.
* **Bluetooth keyboard** — Switch 2's Bluetooth is audio-only.
* **`adb shell input text`** — types into Android's own input stack. The Switch
  is on the other end of a cable; nothing Android does can reach it.
* **A pre-built save file** — SmileBASIC saves are encrypted and console-locked,
  so you cannot author `NS2` on a PC and load it.

So the phone in your pocket, which is already rooted and already has a USB port,
becomes the keyboard. That is the whole idea: `send` is deterministic
(`--hold 10 --gap 40` keeps every keystroke on the same schedule instead of
human jitter), the
keystroke stream is generated from the file and can be **diffed against a known
good run** before it is ever sent, and a mistake becomes a bug in a 300-line
Python file instead of a mystery in a 224-line BASIC listing.

That diffability is not decoration — it is how this repository was validated.
What has been proven on the console is the transport: the indent test and
single-word probes land exactly, with correct case and indentation. What is
checked before every long run is the transcript: the stream generated for the
224-line `bootstrap.bas` diffs clean against the prototype stream.

## Quick start

```bash
cd tools/android-hid-kb

./hidkb.py doctor                        # can this phone be a USB device at all?
./hidkb.py setup                         # create the gadget (port untouched, ADB stays up)
./hidkb.py selftest --verify             # bind, type "hello", restore — watch keycodes locally
./hidkb.py calibrate                     # detect the host's keyboard layout (once per host)
./hidkb.py send examples/punctuation.txt # type a real file
./hidkb.py teardown                      # remove the gadget, give the port back
```

If the host types the *wrong characters* (e.g. `a`→`q`, `1`→`&`), its keyboard
layout is not US-QWERTY. Run `calibrate` once: it types a probe, you read one
line back, and it detects the layout + whether the target inverts Shift, saves
it, and prints the exact `send` command. See **Keyboard layouts** below.

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
| `calibrate` | types a known probe, reads back what the host drew, and auto-detects its keyboard layout + Shift-inversion; saves it for `--layout auto` |

### `send` options

```
--profile plain|smilebasic   smilebasic = inverted Shift on every key + inherited indentation
--case-invert / --no-case-invert   invert Shift on every glyph key (not just letters)
--layout auto|us|fr          host keyboard layout (auto = what `calibrate` detected, else us)
--indent literal|inherit     inherit = send only the *delta* of leading spaces after Enter
--hold MS                    key-down time (default 15)
--gap MS                     between keystrokes (default 60)
--prewait S / --postwait S   extra settle time around the bind
--backspaces N               send N Backspaces first (clears a dirty editor line)
--watchdog S                 forced restore if something hangs (default 1800)
--progress N                 emit a progress line every N keystrokes
--dry-run                    generate the on-device script, send nothing
--out PATH                   keep the generated script
--gadget DIR                 our own configfs gadget dir (mode=own)
--mode auto|own|vendor       auto: own gadget if creatable, else attach HID to the
                             vendor's gadget (default; required on Samsung)
--udc NAME                   UDC name (default: the first in /sys/class/udc)
```

Estimate is printed before anything is sent: `keystrokes × (hold + gap)`.

## Worked example: 7 620 keystrokes into a Nintendo Switch 2

The reason this exists: the ns2jb exploit requires a 224-line BASIC program in
SmileBASIC 4.4.9, and typing it by hand on a console is miserable.

```bash
./hidkb.py doctor                                    # confirm UDC + f_hid
./hidkb.py setup
./hidkb.py calibrate                                 # detect the console's layout (once)
./hidkb.py send bootstrap.bas --layout fr --profile smilebasic
#   → 7620 keystrokes, ~9.5 min at the default 75 ms/key
```

* `--layout fr` sends **French-AZERTY positions** (the tested Switch 2's
  SmileBASIC keyboard is French); drop it or use `--layout us` for a US console.
* `--profile smilebasic` **inverts Shift on every normal key**, because
  SmileBASIC's USB path renders each key with the opposite Shift state (AltGr
  keys like `#` `@` are left alone — see **Keyboard layouts**).
* `--indent inherit` reproduces the editor's habit of carrying the previous
  line's leading spaces into the new line, so indentation is not doubled.
* Proven on hardware: indentation (`0,1,1,2,1,1,0,0`), case handling, and
  short words arrive exactly. The 224-line transcript is verified by diffing it
  against the validated prototype stream before it is sent; the full run is the
  long step, and it is the one that must not be interrupted.

Verify a transfer before using it: `examples/indent-test.bas` must arrive with
leading-space counts `0,1,1,2,1,1,0,0`.

## Keyboard layouts: when the host types the wrong characters

HID carries key **positions**, not characters. Which glyph a position produces
is decided by the **host's** active keyboard layout, and the host never tells you
which one it is. Send US-QWERTY positions to a host set to French AZERTY and you
get `a`→`q`, `z`→`w`, `1`→`&`, `(`→`5` — the right keys, the wrong letters.

So the tool models the host layout and sends the position that yields each wanted
character:

```bash
./hidkb.py calibrate                 # once per host: detect + save its layout
./hidkb.py send file.bas --layout fr # or just --layout auto after calibrating
```

* **`calibrate`** types a 26-letter + 10-digit probe into the host's focused
  field. You read the first line back to it; it scores every known layout, works
  out whether the target inverts Shift, saves the result for `--layout auto`, and
  prints the exact `send` command. One line of readback is enough to tell QWERTY
  from AZERTY.
* **`--layout {auto,us,fr}`** — `us` is the original 1:1 behaviour; `fr` is full
  French AZERTY (ISO), including the AltGr third level (`# @ { } [ ] \ | ~ ^`),
  and covers all printable ASCII. `auto` uses whatever `calibrate` saved (US until
  you calibrate). Add a layout by dropping a `*_ROWS` table into `hidkb.py`.

### SmileBASIC inverts Shift — on every key but AltGr

Verified on a Switch 2 whose SmileBASIC keyboard is French: the editor renders
each key with the **opposite Shift state** to what was sent — and not only for
letters. An unshifted number-row key gives the digit, a shifted one gives the
symbol, the reverse of a PC. `--profile smilebasic` (or `--case-invert`) flips
the Shift bit on every normal glyph key so the editor flips it back and letters,
digits and shifted punctuation all land.

The one exception is **AltGr**: those keys (`#` `@` `{` `}` `[` `]` `\` `|` `~`
`^`) are *not* Shift-inverted by SmileBASIC and render normally — but `AltGr+Shift`
is dropped by the console, so the tool never adds Shift to an AltGr key. This was
found the honest way: a first run printed `#` and `@` fine; adding a blanket
Shift-invert made them vanish; the fix leaves AltGr keys alone.

So the working recipe for a French Switch 2 is:

```bash
./hidkb.py send bootstrap.bas --layout fr --profile smilebasic
```

If your console's keyboard language is set to **English (US)** instead, none of
this applies: `--layout us` maps 1:1 and only `--profile smilebasic` (the Shift
inversion) is needed. Changing the console's keyboard to US is the simplest fix
of all when you can.

## Samsung: why `--mode vendor` exists

Samsung's configfs glue creates a platform device per gadget directory and never
frees it, so after a couple of create/remove cycles `mkdir` fails:

```
$ mkdir /config/usb_gadget/hidkb
mkdir: '/config/usb_gadget/hidkb': Out of memory       <- -EEXIST in disguise
$ dmesg | tail -1
kobject_add_internal failed for android2 with -EEXIST, don't try to register
things with the same name in the same directory.
```

No reboot needed: `--mode vendor` (the `auto` fallback) attaches the HID
function to the gadget the vendor HAL already owns instead. Two more wrinkles
come with that gadget, both handled automatically:

* a function cannot be linked into a config that is **bound**
  (`ln: Invalid argument`), so the link happens inside `bind`, right after the
  gadget is released;
* a bound function's attributes are frozen (`xwrite: Device or resource busy`),
  so `setup` only writes values that are actually wrong.

```bash
./hidkb.py setup --mode vendor    # adds functions/hid.usb0 + configs/b.1/hid.usb0
./hidkb.py send file.txt --mode vendor
./hidkb.py teardown --mode vendor # removes them, hands the stock gadget back
```

## Re-binding the UDC rebooted a phone

This one is not a theory. It happened here, on an SM-S948B (Android 17, kernel
`6.12.69-android16`), while the phone was plugged into a console and **already**
typing-capable:

```
14:23  hidkb.py send probe.txt      <- re-ran bind: echo "" > g1/UDC, then echo UDC > g1/UDC
14:23  phone reboots mid-command
14:24  comes back up, no ADB over Wi-Fi (adb tcpip does not survive a reboot)
```

What the device says afterwards:

| Evidence | Value | Reading |
|---|---|---|
| `/proc/uptime` | 387 s | it really rebooted |
| `ro.boot.bootreason` | `reboot` | a reboot that was *requested*, not `kernel_panic`/`watchdog` |
| `/sys/fs/pstore` | empty | no crashed-kernel trace was captured |
| `/data/tombstones` | nothing new | no native crash at that time |
| PDIC log `POR` | `0` | no power-on / PMIC reset |

The cause is therefore **not proven**. What is proven is that the only
destructive operation in that run was the UDC dance, and that the dance was
**unnecessary**: at the time, `g1/UDC = a600000.dwc3`, UDC state `configured`,
`/dev/hidg0 = 447:0` and the report descriptor was correct — the keyboard was
already live and `send` only had to write reports to `/dev/hidg0`.

I tried to "fix" that by making `send` skip the bind whenever a HID was already
attached. It did not help: the failure that followed was not the bind, it was the
vendor HAL rebuilding its gadget on a USB event (`svc usb setFunctions`) while
keystrokes were still being written to `/dev/hidg0` — `request … was not queued
to ep0out`, then a second reboot. So the code is the plain, proven sequence
again: **bind once at the start of a run, type, restore.** No hidden state, no
opt-in flags.

What that means in practice:

* do not call `svc usb` / `setprop sys.usb.config` while a HID is attached —
  that is what provokes the rebuild;
* do not type into a controller that is not `state=configured`;
* a cable replug is the safe way to get a fresh enumeration.

One concrete stressor *was* found and removed. The on-device sleep used
`read -t <s> < /dev/zero`, which keeps correct time but busy-waits: `/dev/zero`
is always readable, so the shell spins at 100 % of a CPU core for the whole
interval (measured ~2.98 s of CPU per 3 s of sleep). During a run that pinned one
core for the main typing loop and a second for the 30-minute watchdog — minutes
of full load and current draw on a phone powered from a console port, which is
exactly the kind of thermal/brownout condition that produces a `bootreason=reboot`
with an empty pstore. The sleep now reads from a fifo with no writer, which blocks
idle in `poll()` at ~0 % CPU with the same ±1 ms accuracy. This does not *prove*
it caused the reboots, but it was a real, measured load that is now gone.

If you do lose the phone: root survives (KernelSU came back), and

```bash
adb shell su -c 'setprop persist.adb.tcp.port 5555'   # survives future reboots
adb tcpip 5555 && adb connect <phone-ip>:5555
```

costs one trip on the USB cable — which, on a single-port phone, is the port you
wanted for the target.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `couldn't find an available UDC or it's busy` | vendor USB HAL re-bound its gadget | do not add any delay between releasing the old gadget and claiming the UDC; `hidkb-lib.sh` already races it |
| `mkdir … Out of memory` on setup | Samsung leaks a platform device per gadget dir | `--mode vendor` (auto does it) |
| `ln: Invalid argument` | linking a function into a *bound* config | automatic: the link is deferred to `bind` |
| `xwrite: Device or resource busy` | writing attributes of a bound function | automatic: `setup` skips values that already match |
| `hidg0 never appeared` | no `f_hid` in the kernel, or bind never happened | `doctor` (`/sys/class/hidg` only exists while a function is bound) |
| `mknod failed` | not root, or no `/sys/class/hidg/hidg0/dev` | check `id -u`; node is only created after a successful bind |
| host enumerates but nothing is typed | wrong modifier/key slot order, wrong protocol, or the host's text field is not focused | run `selftest --verify` on a Linux host and decode the keycodes |
| characters permuted (`a`↔`q`, `z`↔`w`, `1`↔`&`) | host layout is not US-QWERTY (AZERTY/QWERTZ) | `./hidkb.py calibrate`, then `--layout fr` (or `--layout auto`) |
| digits/symbols shifted (`1`→`&`, `(`→`5`) but letters fine | target inverts Shift on every key (SmileBASIC) | `--profile smilebasic` (or `--case-invert`) |
| `#` `@` `{` `[` … drop out | AltGr key sent with Shift, which the console ignores | automatic: the tool never adds Shift to an AltGr key; update if you see it |
| typed text is uppercase where it should be lowercase | the target inverts case (SmileBASIC) | `--profile smilebasic` or `--case-invert` |
| indentation doubles each line | target editor inherits indentation | `--indent inherit` |
| ADB/MTP gone after a run | gadget still bound | `hidkb.py teardown`; the trap/watchdog normally does it already |
| phone rebooted during a run | UDC re-bind or a HAL gadget rebuild while reports were flowing — cause unproven, twice seen | unplug/replug, confirm `state=configured`, run again; do not issue `svc usb` while a HID is attached |
| phone charges from the host, nothing enumerates | USB role reversed | unplug, replug, or use a data-capable cable |

## Safety

* `setup` never touches the port.
* `send` wraps the bind in `trap 'hidkb_restore; exit 0' INT TERM EXIT`, ignores
  `SIGHUP` (so a Wi-Fi ADB hiccup does not abort mid-transcript), and arms a
  watchdog subshell. The stock gadget is recorded in
  `/data/local/tmp/hidkb-owner` and re-bound automatically.
* Ctrl-C during `send` restores the port from the host side.
* Writing `UDC`, and letting the vendor HAL rebuild its gadget, are the two
  genuinely risky operations. Between them they rebooted a phone twice, with no
  panic trace either time (`bootreason=reboot`, empty pstore). No repair is
  needed to recover: root and the app's data survive the reboot.
* Nothing persistent is written outside `/data/local/tmp` and configfs, so a
  reboot is always a complete reset of this tool's state.

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
