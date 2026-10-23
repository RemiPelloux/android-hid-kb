# Implementation notes — why this is built the way it is

Everything here was learned by poking a **Samsung Galaxy S26 (SM-S948B, Android 17,
kernel 6.12.69-android16-6 GKI)** rooted through [GalaxyCrash](https://github.com/RemiPelloux/GalaxyCrash),
and verified against two hosts: a Linux laptop (`/dev/input`) and a Nintendo Switch 2
(SmileBASIC text editor).

---

## 1. The kernel already has everything; Android just doesn't expose it

Relevant kernel configuration on the tested device:

```
CONFIG_USB_CONFIGFS=y
CONFIG_USB_CONFIGFS_F_HID=y     <- the HID function is built in
CONFIG_USB_HID=y
CONFIG_USB_HIDDEV=y
CONFIG_ANDROID_USB_CONFIGFS_UEVENT=y
```

Two consequences:

* **No module, no compiler, no reboot.** `f_hid` is compiled in, so a configfs
  gadget with a HID function can be created from a shell as root.
* **`CONFIG_ANDROID_USB_CONFIGFS_UEVENT=y` is the reason the vendor HAL fights
  you.** The framework watches configfs through uevents, so any change to the
  gadget tree is noticed immediately.

The available UDC:

```
$ cat /sys/class/udc/*/../uevent   # or simply
$ ls /sys/class/udc/
a600000.dwc3
```

## 2. Problem: the vendor USB HAL owns the UDC

Naively:

```sh
echo "" > /config/usb_gadget/g1/UDC          # release the stock gadget
echo a600000.dwc3 > /config/usb_gadget/hidkb/UDC
```

fails, and the configfs read-back still shows the stock gadget bound. `dmesg`
shows why:

```
configfs-gadget.g1 gadget.0: reset acm ttyGS0
configfs-gadget.g1 gadget.0: activate acm ttyGS0
UDC core: hidkb: couldn't find an available UDC or it's busy
```

`android.hardware.usb-service.qti` (`init.svc.vendor.usb-hal`) reacted to the
unbind within a few hundred milliseconds and re-bound `g1` before we got there.

**Fix: win a zero-delay race.** Release and claim back-to-back, with no sleep,
no `cat`, no logging in between:

```sh
echo "" > "$OWNER/UDC"
echo "$UDC" > "$HIDKB_GADGET/UDC"
```

The HAL's own attempt then loses:

```
UDC core: g1: couldn't find an available UDC or it's busy
```

An earlier attempt using `sleep 0.5` for "safety" is exactly what made it fail.
Do not add latency between those two writes.

`GADGET/UDC` is also the authoritative check — read it back, don't trust the
exit code (writing to an already-bound UDC fails but the shell only sees `1`).

**Caveat: the race is the riskiest thing this tool does.** On 2026-10-23 a
re-bind of the vendor gadget on an SM-S948B (Android 17, kernel
`6.12.69-android16`) rebooted the phone, mid-`send`, with
`ro.boot.bootreason=reboot`, an empty pstore, no tombstone, and PDIC `POR:0` —
so no kernel panic was recorded and the cause is unproven. The re-bind was also
unnecessary: the gadget was already `configured` with a live `/dev/hidg0`.
`hidkb_bind()` therefore checks `hidkb_attached()` (function bound, linked in a
bound config, `dev` present, `UDC` equal to the detected controller) and skips
the writes above entirely; `--rebind` forces the old path. See the README
section "Re-binding the UDC rebooted a phone".

## 2b. Problem: you cannot always create a gadget directory

Only after this worked for a while did the next trap appear. Any
`mkdir /config/usb_gadget/<name>` started failing:

```
mkdir: '/config/usb_gadget/hidkb': Out of memory
```

`dmesg` after the failure names the real error:

```
kobject_add_internal failed for android2 with -EEXIST, don't try to register
things with the same name in the same directory.
    android_device_create+0xa0/0x138
    gadgets_make+0x26c/0x328
```

Samsung's patched `gadgets_make()` calls `android_device_create()` for every
gadget directory and never destroys the device, so
`/sys/devices/virtual/android_usb/androidN` accumulates and the name lookup
collides. `-EEXIST` surfaces to userspace as `ENOMEM`, which sends you looking
for a memory problem that does not exist. Fixing it requires a reboot, and on a
locked bootloader that means redoing the root exploit.

**Fix: don't create a gadget at all.** The vendor HAL already owns a complete,
enumerating gadget (`g1`, config `b.1`). Add a `hid.usb0` function to it, link
it into the existing config, and re-bind the same gadget. The HAL racing us is
now harmless — it can only ever bind that same gadget back.

This became `--mode vendor`, and `auto` falls back to it.

### Corollary: a bound config is frozen

Two write paths that worked on a private gadget fail on the vendor's, because
its gadget is *bound* to the UDC almost all the time:

```
# linking a function into a config that is bound
ln: cannot create symbolic link ... : Invalid argument        # -EINVAL

# writing an attribute of a bound function
cat: xwrite: Device or resource busy                          # -EBUSY
```

Both are handled by ordering rather than by retrying:

* the link is made inside `hidkb_bind()`, in the window after the incumbent
  gadget is released and before ours is bound;
* `setup` compares each attribute with the desired value and only writes when it
  differs, so it never touches a live function.

## 3. Problem: `/dev/hidg0` does not exist

The kernel creates the char device and a `hidg` class entry — **only while a HID
function is bound**. When the last one goes away, `/sys/class/hidg` disappears
entirely, so it is not a usable "is f_hid compiled in?" probe; read
`/proc/config.gz` for that. Also, the id is global and not reused immediately,
so never assume `/dev/hidg0`:

```
$ ls /sys/class/hidg/
hidg0
$ cat /sys/class/hidg/hidg0/dev
447:0
```

but Android has **no udev**; `ueventd` only creates nodes listed in its `.rc`
files, and `hidg` isn't one of them. So `hidkb-lib.sh` mknods it from the
major:minor in sysfs (never hardcode `447:0` — it is allocated dynamically).
The gadget's own function directory is the authoritative source, because the
class device may be `hidg1`, `hidg2`, …:

```sh
cat /config/usb_gadget/hidkb/functions/hid.usb0/dev     # -> 447:0
mknod /dev/hidg0 c 447 0 && chmod 660 /dev/hidg0
```

## 4. Problem: no scripting runtime, and forks are expensive

The device has `sh` (mksh R59), `toybox`, and no python/perl/busybox. Measured
on the device:

| operation | cost |
|---|---|
| `usleep 1000` (external, forks) | **8.3 ms** |
| `printf … > /dev/null` (external, forks) | **6.5 ms** |
| `print -n … > /dev/null` (mksh **builtin**) | **0.03 ms** |
| `read -t 0.05 </dev/zero` (mksh **builtin**) | **50 ms ±1 ms** |

A 7 620-keystroke transcript needs ~15 240 reports. With forks that is ~2 min of
pure `fork()` overhead *plus* the real delays, and jitter that corrupts typing.
Using two mksh builtins costs nothing:

```sh
print -n '\0002\0000\0013\0000\0000\0000\0000\0000' >&4   # Shift+h down
read -t 0.010 x < /dev/zero                                # builtin sleep
print -n '\0000…'                                        >&4   # all keys up
read -t 0.040 x < /dev/zero
```

Notes:

* mksh's `print` builtin understands **`\0NNN` octal only** — `\xNN` and bare
  `\NNN` are emitted literally. `\0002` = `0x02`, `\0377` = `0xff`.
* `read -t` accepts fractional seconds and is accurate to ~1 ms. Reading from
  `/dev/zero` never yields a newline, so it always times out; shell RSS stayed at
  3.4 MB over 1 000 iterations, so there is no buffer blow-up.
* `printf` is **not** a builtin here (`type printf` → tracked alias to
  `/system/bin/printf`), so it must not be used in the hot path.
* Open the endpoint once (`exec 4> /dev/hidg0`) and write with `>&4`; reopening
  per report is another syscall pair per keystroke.

## 5. The report format

An 8-byte boot keyboard report, written to `/dev/hidg0`:

| byte | meaning |
|---|---|
| 0 | modifier bitmap: `01` LCTRL, `02` LSHIFT, `04` LALT, `08` LGUI |
| 1 | reserved (0) |
| 2..7 | up to six HID usage IDs, `0` = none |

Key down, then a report of all zeros = key up. Usage IDs are **US-QWERTY
positions**, not characters: `0x04`=A … `0x1D`=Z, `0x1E`=1 … `0x27`=0.

A host maps positions through its own layout. The Switch is QWERTY, so output
matched the source byte for byte; on an AZERTY host, `A`/`Q`/`W`/`Z` and
`$`/`M` would come out permuted.

## 6. Verifying a report stream (worth doing every time)

The fastest ground truth is a normal Linux host. `pc/watch_input.py` watches for
a new `/dev/input/event*` node and decodes the kernel's view of the reports.
A correct `hello` looks like this:

```
new device: /dev/input/event21

  LEFTSHIFT    down
  H            down
  LEFTSHIFT    up
  H            up
  …
  ENTER        down
  ENTER        up
```

This caught a real bug immediately: the first version swapped the modifier and
key slots, so the host saw `LEFTCTRL+LEFTSHIFT+LEFTMETA` chords instead of
letters — garbage that a console silently ignores, but that a PC reports plainly.

## 7. Power direction

If the host is charging the phone, enumeration can still work, but the phone's
role must be *device*. On the Switch, both USB-C ports work with a data-capable
cable; if the keyboard is not seen, unplug and replug to reset the role.

## 8. Restoring the port

The gadget takes the UDC away from Android, so ADB-over-USB and MTP stop while
it is bound. `hidkb_restore` unbinds and re-binds the stock gadget (recorded in
`/data/local/tmp/hidkb-owner`), so ADB comes back without a replug. It is wired
to `trap … INT TERM EXIT` and to a watchdog, because a half-restored phone with
no ADB is unpleasant.

Over Wi-Fi ADB the disconnect does not even matter: `adb tcpip 5555` +
`adb connect <ip>:5555` keeps the control channel alive while the port is busy.

## 9. Keyboard layouts on editors that "inherit" indentation

Some line editors (SmileBASIC is one) carry the previous line's leading spaces
into the new line after Enter. Sending leading spaces literally then doubles the
indentation. `--indent inherit` computes only the delta — spaces when the next
line is deeper, Backspaces when it is shallower. It never uses Home/End and never
guesses from keywords.

## 10. Case inversion

SmileBASIC's editor maps an unshifted letter key to an **uppercase** character
(the on-screen keyboard is the normal path, the USB keyboard path inverts). So
typing `PRINT` literally yields `print`. The `smilebasic` profile sends Shift for
lowercase letters in the source file, which reproduces the file exactly.

---

### Timing budget

| step | time |
|---|---|
| gadget bind | ~10 ms |
| one keystroke (`--hold 10 --gap 40`) | 50 ms |
| 7 620 keystrokes | ~6.5 min |
| `hello` smoke test | ~1 s |

### Reference material

* Linux `Documentation/usb/gadget_configfs.rst`, `gadget_hid.rst`
* HID 1.11 §B.1 "Boot Interface Descriptors" (the 63-byte descriptor above)
* [`cpypwn/mac2jb`](https://github.com/cpypwn/mac2jb) — the macOS Apple-silicon
  equivalent, and the source of the inherited-indentation and case-inversion
  tricks
* [`pepsipu/ns2jb`](https://gitlab.com/pepsipu/ns2jb) — the Switch 2 exploit
  whose 7 620-keystroke bootstrap motivated building this
