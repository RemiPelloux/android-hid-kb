#!/system/bin/sh
# hidkb-lib.sh — USB HID keyboard gadget helpers for rooted Android (mksh + toybox).
#
# Sourced on-device by generated senders and by hidkb.py. Every function is
# idempotent: calling it twice is harmless.
#
# Four constraints shaped this file (see docs/implementation.md):
#
#   1. No python/perl/busybox on stock Android  -> mksh builtins only.
#   2. fork()+exec() costs ~7 ms on Android     -> nothing in the hot path forks.
#   3. Android has no udev                      -> /dev/hidg0 must be mknod'ed.
#   4. The vendor USB HAL owns the UDC          -> it must be preempted, not asked.
#      Writing "" to its UDC is undone within a few ms by
#      android.hardware.usb-service.*, so the bind has to win a zero-delay race.

HIDKB_GADGET=${HIDKB_GADGET:-/config/usb_gadget/hidkb}
HIDKB_HOST_GADGET=${HIDKB_HOST_GADGET:-}
HIDKB_UDC=${HIDKB_UDC:-}
HIDKB_DESC=${HIDKB_DESC:-/data/local/tmp/hidkb-report_desc.bin}
HIDKB_LOG=${HIDKB_LOG:-/data/local/tmp/hidkb.log}

hidkb_msg() { echo "hidkb: $*"; }

# Fractional sleep with no subprocess. mksh's `read -t` is accurate to ~1 ms
# and reading from /dev/zero never delivers a newline, so it always times out.
# Measured: 100 calls of 0.04 s = 4040 ms, shell RSS stable at 3.4 MB.
hidkb_nap() { read -t "$1" _hidkb_sleep < /dev/zero 2>/dev/null; return 0; }

hidkb_all_gadgets() { ls -d /config/usb_gadget/*/ 2>/dev/null; }

# Print the path of the gadget currently bound to $HIDKB_UDC ("" if none).
hidkb_owner() {
	for g in $(hidkb_all_gadgets); do
		[ "$(cat "$g/UDC" 2>/dev/null)" = "$HIDKB_UDC" ] && { echo "${g%/}"; return 0; }
	done
	return 1
}

hidkb_detect_udc() {
	[ -n "$HIDKB_UDC" ] && return 0
	for u in /sys/class/udc/*; do
		[ -e "$u" ] || continue
		HIDKB_UDC=${u##*/}
		return 0
	done
	hidkb_msg "no USB device controller in /sys/class/udc (host-only port?)"
	return 1
}

# Create the gadget + HID function. Does NOT touch the UDC, so it is safe to run
# while the phone is plugged in and ADB is live.
hidkb_setup_gadget() {
	hidkb_detect_udc || return 1
	G=$HIDKB_GADGET
	[ -d /config/usb_gadget ] || { hidkb_msg "configfs not mounted at /config"; return 1; }
	[ -d "$G" ] || mkdir -p "$G" || { hidkb_msg "cannot create $G"; return 1; }

	echo 0x1209 >"$G/idVendor"     # pid.codes (open-source hardware VID)
	echo 0x0001 >"$G/idProduct"    # pid.codes test PID
	echo 0x0100 >"$G/bcdDevice"
	echo 0x0200 >"$G/bcdUSB"

	mkdir -p "$G/strings/0x409" "$G/configs/c.1/strings/0x409"
	echo HIDKB >"$G/strings/0x409/serialnumber"
	echo hidkb >"$G/strings/0x409/manufacturer"
	echo "HID Keyboard" >"$G/strings/0x409/product"
	echo HID >"$G/configs/c.1/strings/0x409/configuration"
	echo 250 >"$G/configs/c.1/MaxPower"

	F=$G/functions/hid.usb0
	mkdir -p "$F" || return 1
	echo 1 >"$F/protocol"       # bInterfaceProtocol: 1 = keyboard (boot)
	echo 1 >"$F/subclass"       # bInterfaceSubClass: 1 = boot interface
	echo 8 >"$F/report_length"  # 8-byte boot keyboard report
	if [ -s "$HIDKB_DESC" ]; then
		cat "$HIDKB_DESC" >"$F/report_desc" || { hidkb_msg "report_desc write failed"; return 1; }
	else
		hidkb_msg "missing $HIDKB_DESC (push it before setup)"
		return 1
	fi
	[ -e "$G/configs/c.1/hid.usb0" ] || ln -s "$F" "$G/configs/c.1/hid.usb0"
	hidkb_msg "gadget $G created (not bound)"
}

# Bind the gadget to the UDC. This is the only disruptive step: it steals the
# port from the vendor gadget, so ADB-over-USB and MTP die while it is bound.
hidkb_bind() {
	hidkb_detect_udc || return 1
	[ -d "$HIDKB_GADGET" ] || { hidkb_msg "run setup first"; return 1; }

	OWNER=$(hidkb_owner)
	HIDKB_HOST_GADGET=${HIDKB_HOST_GADGET:-$OWNER}
	[ -n "$OWNER" ] && echo "$OWNER" > /data/local/tmp/hidkb-owner

	# Zero-delay race: release the incumbent, then claim the UDC immediately.
	# Any gap >= a few ms lets the vendor HAL re-bind first and we lose with
	# "UDC core: <gadget>: couldn't find an available UDC or it's busy".
	[ -n "$OWNER" ] && echo "" >"$OWNER/UDC" 2>/dev/null
	echo "$HIDKB_UDC" >"$HIDKB_GADGET/UDC" 2>/dev/null
	[ "$(cat "$HIDKB_GADGET/UDC" 2>/dev/null)" = "$HIDKB_UDC" ] || {
		hidkb_msg "lost the UDC race (still owned by $(hidkb_owner))"
		return 1
	}

	hidkb_wait_hidg || return 1
	hidkb_mknod || return 1
	hidkb_msg "bound $HIDKB_UDC, /dev/hidg0 ready ($(cat /sys/class/udc/"$HIDKB_UDC"/state 2>/dev/null))"
	return 0
}

# Android's ueventd has no rule for the hidg class, so the node is missing even
# though the kernel created the device. Major:minor comes from sysfs (447:0 on
# the tested 6.1 GKI kernel, but never hardcode it).
# major:minor of our HID char device.
#
# Never assume hidg0: the class device id is allocated globally and a function
# from an earlier session leaves its id taken, so the next one shows up as
# hidg1, hidg2, ... The gadget's own function directory is authoritative.
hidkb_hidg_dev() {
	for f in "$HIDKB_GADGET"/functions/hid.*/dev; do
		[ -e "$f" ] || continue
		v=$(cat "$f" 2>/dev/null)
		[ -n "$v" ] && { echo "$v"; return 0; }
	done
	for f in /sys/class/hidg/hidg*/dev; do
		[ -e "$f" ] || continue
		v=$(cat "$f" 2>/dev/null)
		[ -n "$v" ] && { echo "$v"; return 0; }
	done
	return 1
}

hidkb_wait_hidg() {
	i=0
	while :; do
		hidkb_hidg_dev >/dev/null 2>&1 && return 0
		[ $i -ge 100 ] && { hidkb_msg "HID char device never appeared"; return 1; }
		hidkb_nap 0.05
		i=$((i + 1))
	done
}

hidkb_mknod() {
	dev=$(hidkb_hidg_dev) || { hidkb_msg "no major:minor for the HID device"; return 1; }
	maj=${dev%:*}
	min=${dev#*:}
	# Drop a stale node first: a leftover /dev/hidg0 from an earlier run can point
	# at a dead major:minor, and mknod cannot overwrite an existing file.
	rm -f /dev/hidg0
	mknod /dev/hidg0 c "$maj" "$min" || { hidkb_msg "mknod failed"; return 1; }
	chmod 660 /dev/hidg0 2>/dev/null
	hidkb_msg "/dev/hidg0 -> $maj:$min"
	return 0
}

# Give the port back to whoever had it, so ADB/MTP return without a replug.
hidkb_restore() {
	G=$HIDKB_GADGET
	[ -f /data/local/tmp/hidkb-owner ] && HIDKB_HOST_GADGET=${HIDKB_HOST_GADGET:-$(cat /data/local/tmp/hidkb-owner)}
	[ "$(cat "$G/UDC" 2>/dev/null)" = "$HIDKB_UDC" ] && echo "" >"$G/UDC" 2>/dev/null
	hidkb_nap 0.3
	if [ -n "$HIDKB_HOST_GADGET" ] && [ -d "$HIDKB_HOST_GADGET" ]; then
		echo "$HIDKB_UDC" >"$HIDKB_HOST_GADGET/UDC" 2>/dev/null
	fi
	hidkb_msg "restored ${HIDKB_HOST_GADGET:-nothing}"
}

# configfs refuses to delete its own default attributes (os_desc/*, webusb/*), so
# `rm -rf` cannot remove a gadget. Remove in dependency order instead, and treat
# a leftover directory as harmless: an unbound gadget owns no resources.
hidkb_teardown() {
	G=$HIDKB_GADGET
	if [ "$(cat "$G/UDC" 2>/dev/null)" = "$HIDKB_UDC" ]; then
		echo "" >"$G/UDC" 2>/dev/null
	fi
	hidkb_nap 0.3
	hidkb_restore
	rm -f "$G"/configs/*/hid.usb0 "$G"/configs/*/hid.* 2>/dev/null
	rmdir "$G"/configs/*/strings/0x409 "$G"/configs/*/strings "$G"/configs/* 2>/dev/null
	rmdir "$G"/functions/hid.* "$G"/functions 2>/dev/null
	rmdir "$G"/strings/0x409 "$G"/strings 2>/dev/null
	rm -f /dev/hidg0
	if rmdir "$G" 2>/dev/null; then
		hidkb_msg "gadget removed"
	else
		hidkb_msg "gadget unbound but $G still exists (configfs keeps os_desc/webusb)"
	fi
}

# Write one 8-byte boot-protocol report. $1 = octal-escape report string.
hidkb_report() { print -n "$1" >&4; }
