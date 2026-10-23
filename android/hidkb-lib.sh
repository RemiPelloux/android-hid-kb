#!/system/bin/sh
# hidkb-lib.sh — USB HID keyboard gadget helpers for rooted Android (mksh + toybox).
#
# Sourced on-device by generated senders and by hidkb.py. Every function is
# idempotent: calling it twice is harmless.
#
# Five constraints shaped this file (see docs/implementation.md):
#
#   1. No python/perl/busybox on stock Android  -> mksh builtins only.
#   2. fork()+exec() costs ~7 ms on Android     -> nothing in the hot path forks.
#   3. Android has no udev                      -> /dev/hidg0 must be mknod'ed.
#   4. The vendor USB HAL owns the UDC          -> it must be preempted, not asked.
#      Writing "" to its UDC is undone within a few ms by
#      android.hardware.usb-service.*, so the bind has to win a zero-delay race.
#   5. Samsung's configfs glue leaks a platform device per gadget directory
#      (/sys/devices/virtual/android_usb/androidN). After a couple of
#      create/remove cycles gadget mkdir fails with a misleading
#      "Out of memory" (-EEXIST in disguise). Two modes therefore exist:
#        own    - create /config/usb_gadget/hidkb (clean, needs no leak-free mkdir)
#        vendor - attach the HID function to the gadget the vendor HAL owns (g1)
#      auto picks own when it can and falls back to vendor.

HIDKB_GADGET=${HIDKB_GADGET:-/config/usb_gadget/hidkb}
HIDKB_MODE=${HIDKB_MODE:-auto}
HIDKB_HOST_GADGET=${HIDKB_HOST_GADGET:-}
HIDKB_UDC=${HIDKB_UDC:-}
HIDKB_DESC=${HIDKB_DESC:-/data/local/tmp/hidkb-report_desc.bin}
HIDKB_LOG=${HIDKB_LOG:-/data/local/tmp/hidkb.log}
HIDKB_STATE=${HIDKB_STATE:-/data/local/tmp/hidkb-active}
HIDKB_OWNER_FILE=${HIDKB_OWNER_FILE:-/data/local/tmp/hidkb-owner}
HIDKB_ACTIVE=${HIDKB_ACTIVE:-}
HIDKB_CONFIG=${HIDKB_CONFIG:-}

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

# The vendor HAL's own gadget, if we can recognise it: prefer the bound one,
# then the conventional name, then whatever exists.
hidkb_first_gadget() {
	g=$(hidkb_owner) && { echo "$g"; return 0; }
	[ -d /config/usb_gadget/g1 ] && { echo /config/usb_gadget/g1; return 0; }
	for d in $(hidkb_all_gadgets); do echo "${d%/}"; return 0; done
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

# --------------------------------------------------------------------------- #
# gadget selection: which gadget directory are we augmenting?
# --------------------------------------------------------------------------- #

hidkb_state_save() {
	echo "$HIDKB_ACTIVE" >"$HIDKB_STATE" 2>/dev/null
	echo "$HIDKB_CONFIG" >>"$HIDKB_STATE" 2>/dev/null
	return 0
}

hidkb_state_load() {
	[ -n "$HIDKB_ACTIVE" ] && return 0
	[ -f "$HIDKB_STATE" ] || return 1
	{ read -r a; read -r c; } <"$HIDKB_STATE"
	[ -n "$a" ] && [ -d "$a" ] || return 1
	HIDKB_ACTIVE=$a
	HIDKB_CONFIG=${c:-$a/configs/c.1}
	return 0
}

hidkb_use_own() {
	[ -d "$HIDKB_GADGET" ] || mkdir "$HIDKB_GADGET" 2>/dev/null
	[ -d "$HIDKB_GADGET" ] || return 1
	HIDKB_ACTIVE=$HIDKB_GADGET
	HIDKB_CONFIG=$HIDKB_GADGET/configs/c.1
	return 0
}

hidkb_use_vendor() {
	g=$(hidkb_first_gadget) || return 1
	[ -n "$g" ] || return 1
	c=
	for d in "$g"/configs/*/; do
		[ -d "$d" ] && { c=${d%/}; break; }
	done
	[ -n "$c" ] || { hidkb_msg "$g has no configuration directory"; return 1; }
	HIDKB_ACTIVE=$g
	HIDKB_CONFIG=$c
	return 0
}

# Selection used by setup: may create the private gadget.
hidkb_prepare() {
	hidkb_state_load && return 0
	if [ "$HIDKB_MODE" = vendor ]; then
		hidkb_use_vendor
		return $?
	fi
	hidkb_use_own && return 0
	if [ "$HIDKB_MODE" = own ]; then
		hidkb_msg "cannot create $HIDKB_GADGET (Samsung leaks one device per gadget dir: 'Out of memory' means -EEXIST)"
		return 1
	fi
	hidkb_msg "cannot create $HIDKB_GADGET; attaching HID to the existing gadget instead"
	hidkb_use_vendor
}

# Selection used by bind/restore/teardown: never creates anything.
hidkb_locate() {
	hidkb_state_load && return 0
	if [ -d "$HIDKB_GADGET" ]; then
		HIDKB_ACTIVE=$HIDKB_GADGET
		HIDKB_CONFIG=$HIDKB_GADGET/configs/c.1
		return 0
	fi
	hidkb_use_vendor
}

# --------------------------------------------------------------------------- #
# setup
# --------------------------------------------------------------------------- #

_hidkb_describe_own_gadget() {
	G=$HIDKB_ACTIVE
	echo 0x1209 >"$G/idVendor"     # pid.codes (open-source hardware VID)
	echo 0x0001 >"$G/idProduct"    # pid.codes test PID
	echo 0x0100 >"$G/bcdDevice"
	echo 0x0200 >"$G/bcdUSB"

	mkdir -p "$G/strings/0x409" "$HIDKB_CONFIG/strings/0x409" || return 1
	echo HIDKB >"$G/strings/0x409/serialnumber"
	echo hidkb >"$G/strings/0x409/manufacturer"
	echo "HID Keyboard" >"$G/strings/0x409/product"
	echo HID >"$HIDKB_CONFIG/strings/0x409/configuration"
	echo 250 >"$HIDKB_CONFIG/MaxPower"
	return 0
}

# configfs freezes a function's attributes once it is bound (write fails with
# EBUSY), so only write when the current value is actually wrong.
_hidkb_set_attr() {
	[ "$(cat "$1" 2>/dev/null)" = "$2" ] && return 0
	echo "$2" >"$1" 2>/dev/null || return 1
	return 0
}

_hidkb_ensure_link() {
	[ -e "$HIDKB_CONFIG/hid.usb0" ] && return 0
	F=$HIDKB_ACTIVE/functions/hid.usb0
	[ -d "$F" ] || mkdir -p "$F" 2>/dev/null
	[ -d "$F" ] || return 1
	[ -s "$F/report_desc" ] || cat "$HIDKB_DESC" >"$F/report_desc" 2>/dev/null
	ln -s "$F" "$HIDKB_CONFIG/hid.usb0" 2>/dev/null
}

_hidkb_create_function() {
	G=$HIDKB_ACTIVE
	F=$G/functions/hid.usb0
	[ -d "$F" ] || mkdir -p "$F" || { hidkb_msg "cannot create $F"; return 1; }
	[ -s "$HIDKB_DESC" ] || { hidkb_msg "missing $HIDKB_DESC (push it before setup)"; return 1; }
	# bInterfaceProtocol: 1 = keyboard (boot); subClass 1 = boot interface.
	_hidkb_set_attr "$F/protocol" 1 || { hidkb_msg "protocol write failed"; return 1; }
	_hidkb_set_attr "$F/subclass" 1 || { hidkb_msg "subclass write failed"; return 1; }
	_hidkb_set_attr "$F/report_length" 8 || { hidkb_msg "report_length write failed"; return 1; }
	if ! cat "$HIDKB_DESC" >"$F/report_desc" 2>/dev/null; then
		# Bound functions reject writes; a non-empty descriptor means ours is
		# already installed. The end-to-end selftest is the real proof.
		if [ -n "$(cat "$F/report_desc" 2>/dev/null)" ]; then
			hidkb_msg "report descriptor already installed (function is live)"
		else
			hidkb_msg "report_desc write failed"
			return 1
		fi
	fi
	mkdir -p "$HIDKB_CONFIG" 2>/dev/null
	# configfs refuses to link a function into a config that is currently bound
	# ("ln: Invalid argument" / -EINVAL), which is always the case for the vendor
	# gadget. Defer the link to hidkb_bind(), where the gadget is already unbound.
	_hidkb_ensure_link || hidkb_msg "HID link deferred to bind (config is bound right now)"
	return 0
}

# Create the gadget + HID function. Does NOT touch the UDC, so it is safe to run
# while the phone is plugged in and ADB is live.
hidkb_setup_gadget() {
	hidkb_detect_udc || return 1
	[ -d /config/usb_gadget ] || { hidkb_msg "configfs not mounted at /config"; return 1; }
	hidkb_prepare || return 1
	if [ "$HIDKB_ACTIVE" = "$HIDKB_GADGET" ]; then
		_hidkb_describe_own_gadget || return 1
	fi
	_hidkb_create_function || return 1
	hidkb_state_save
	hidkb_msg "HID function ready on $HIDKB_ACTIVE (${HIDKB_CONFIG#"$HIDKB_ACTIVE"/}), not bound"
}

# --------------------------------------------------------------------------- #
# bind / restore / teardown
# --------------------------------------------------------------------------- #

# Bind the gadget to the UDC. This is the only disruptive step: it steals the
# port from the vendor gadget, so ADB-over-USB and MTP die while it is bound.
# True when our HID function is already live on a bound gadget. Writing to UDC in
# that state is pure risk — re-binding the controller rebooted a Samsung phone
# once (see README, "Re-binding the UDC rebooted a phone") — so hidkb_bind()
# reuses the live attachment instead of tearing the controller down.
hidkb_attached() {
	[ -n "${HIDKB_ACTIVE:-}" ] && [ -n "${HIDKB_UDC:-}" ] || return 1
	[ -s "$HIDKB_ACTIVE/functions/hid.usb0/dev" ] || return 1
	[ -e "$HIDKB_CONFIG/hid.usb0" ] || return 1
	[ "$(cat "$HIDKB_ACTIVE/UDC" 2>/dev/null)" = "$HIDKB_UDC" ] || return 1
	return 0
}
hidkb_bind() {
	hidkb_detect_udc || return 1
	hidkb_locate || { hidkb_msg "run setup first"; return 1; }
	G=$HIDKB_ACTIVE

	OWNER=$(hidkb_owner)
	HIDKB_HOST_GADGET=${HIDKB_HOST_GADGET:-$OWNER}
	[ -n "$OWNER" ] && print -r -- "$OWNER" >"$HIDKB_OWNER_FILE" 2>/dev/null

	# The function is already bound and the host has enumerated it: keep it. The
	# UDC write below is what kills the vendor HAL (and once, the whole phone).
	if [ "${HIDKB_REBIND:-0}" != 1 ] && hidkb_attached; then
		hidkb_mknod || return 1
		hidkb_msg "HID already attached to $HIDKB_UDC, leaving the UDC alone"
		return 0
	fi

	# Zero-delay race: release the incumbent, then claim the UDC immediately.
	# Any gap >= a few ms lets the vendor HAL re-bind first and we lose with
	# "UDC core: <gadget>: couldn't find an available UDC or it's busy".
	# When we are the incumbent (vendor mode) the HAL can only re-bind this same
	# gadget, so the race cannot be lost.
	# Keep the kernel log across a bad roll: dmesg dies with the boot, /data does not.
	# If the UDC write below ever reboots the device again, this file is the only
	# surviving evidence of what the gadget/HAL were doing at the time.
	[ -d /data/local/tmp ] && dmesg >/data/local/tmp/hidkb-pre-bind-dmesg.txt 2>/dev/null
	[ -n "$OWNER" ] && echo "" >"$OWNER/UDC" 2>/dev/null
	# Now that the gadget is unbound the config accepts the HID function link.
	_hidkb_ensure_link || {
		hidkb_msg "cannot attach the HID function to $HIDKB_CONFIG"
		[ -n "$OWNER" ] && echo "$HIDKB_UDC" >"$OWNER/UDC" 2>/dev/null
		return 1
	}
	echo "$HIDKB_UDC" >"$G/UDC" 2>/dev/null
	[ "$(cat "$G/UDC" 2>/dev/null)" = "$HIDKB_UDC" ] || {
		hidkb_msg "lost the UDC race (still owned by $(hidkb_owner))"
		return 1
	}

	hidkb_wait_hidg || return 1
	hidkb_mknod || return 1
	hidkb_msg "bound $HIDKB_UDC, /dev/hidg0 ready ($(cat /sys/class/udc/"$HIDKB_UDC"/state 2>/dev/null))"
	return 0
}

# major:minor of our HID char device.
#
# Never assume hidg0: the class device id is allocated globally and a function
# from an earlier session leaves its id taken, so the next one shows up as
# hidg1, hidg2, ... The gadget's own function directory is authoritative.
hidkb_hidg_dev() {
	for f in "${HIDKB_ACTIVE:-$HIDKB_GADGET}"/functions/hid.*/dev; do
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
	hidkb_locate || return 0
	G=$HIDKB_ACTIVE
	[ -f "$HIDKB_OWNER_FILE" ] && HIDKB_HOST_GADGET=${HIDKB_HOST_GADGET:-$(cat "$HIDKB_OWNER_FILE")}
	[ "$(cat "$G/UDC" 2>/dev/null)" = "$HIDKB_UDC" ] && echo "" >"$G/UDC" 2>/dev/null
	hidkb_nap 0.3
	if [ -n "$HIDKB_HOST_GADGET" ] && [ -d "$HIDKB_HOST_GADGET" ]; then
		echo "$HIDKB_UDC" >"$HIDKB_HOST_GADGET/UDC" 2>/dev/null
	fi
	hidkb_msg "restored ${HIDKB_HOST_GADGET:-nothing}"
}

# Detach the HID function. In vendor mode the vendor gadget itself stays, only
# our function and its link are removed, and the host is re-enumerated stock.
hidkb_teardown() {
	hidkb_locate || { hidkb_msg "nothing to tear down"; return 0; }
	G=$HIDKB_ACTIVE
	[ -n "$G" ] && [ -d "$G" ] || { hidkb_msg "nothing to tear down"; return 0; }

	[ "$(cat "$G/UDC" 2>/dev/null)" = "$HIDKB_UDC" ] && echo "" >"$G/UDC" 2>/dev/null
	hidkb_nap 0.3

	rm -f "$HIDKB_CONFIG"/hid.usb0 "$HIDKB_CONFIG"/hid.* 2>/dev/null
	rmdir "$G"/functions/hid.* 2>/dev/null
	rm -f /dev/hidg0 "$HIDKB_STATE" 2>/dev/null

	if [ "$G" = "$HIDKB_GADGET" ]; then
		# configfs keeps its own default attributes (os_desc/*, webusb/*), so a
		# gadget directory cannot be `rm -rf`'d. Remove in dependency order and
		# treat a leftover directory as harmless: unbound, it owns nothing.
		rmdir "$G"/configs/*/strings/0x409 "$G"/configs/*/strings "$G"/configs/* 2>/dev/null
		rmdir "$G"/functions 2>/dev/null
		rmdir "$G"/strings/0x409 "$G"/strings 2>/dev/null
		if rmdir "$G" 2>/dev/null; then
			hidkb_msg "gadget removed"
		else
			hidkb_msg "gadget unbound, $G left behind (configfs keeps os_desc/webusb)"
		fi
	else
		# Hand the vendor gadget back, now without our function.
		hidkb_nap 0.2
		echo "$HIDKB_UDC" >"$G/UDC" 2>/dev/null
		hidkb_msg "HID function detached from $G and port restored"
	fi
	HIDKB_ACTIVE=
	HIDKB_CONFIG=
	return 0
}

# Write one 8-byte boot-protocol report. $1 = octal-escape report string.
hidkb_report() { print -n "$1" >&4; }
