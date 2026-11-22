#!/usr/bin/env bash
# Keeps this phone's wireless-debugging adb connection alive.
#
# The port changes whenever Wireless debugging is turned off and on again, so a
# connection that worked earlier simply disappears. This loop checks the adb
# device list, and while there is no device it runs `adb-connect`, which asks
# mDNS for the current `_adb-tls-connect._tcp` port and connects to it.
#
#   adb-watch                        # run in the foreground
#   adb-watch --status               # is it running, and is a device attached
#   kill "$(cat out/adb-watch.pid)"  # stop a running watcher
#
# From a checkout, `python3 -m adb_harness.watch` does the same.
#
# Environment: ADB_WATCH_INTERVAL (seconds, default 15), ADB_WATCH_OUT (the
# directory holding the pid and log, default out/ in the working directory) and
# ADB_WATCH_LOG (default `$ADB_WATCH_OUT/adb-watch.log`). State changes are
# logged; a repeated failure is logged again only when the message changes or ten
# minutes have passed, so a phone left with Wireless debugging off does not fill
# the log.
set -u

# Where the pid and the log go: the caller's scratch directory when it names one,
# otherwise out/ in the working directory. This is the installed package's copy,
# so its own directory is not a sensible place to write.
out=${ADB_WATCH_OUT:-$PWD/out}
mkdir -p "$out"

# Reconnecting is adb-connect's job. Installed, that is its command on PATH (the
# adb-watch entry point puts the environment's bin directory there); from a
# checkout, it is the module beside this file, run by the interpreter that started
# the watcher when there is one.
if command -v adb-connect >/dev/null 2>&1; then
    connect=(adb-connect)
else
    connect=("${ADB_HARNESS_PYTHON:-python3}" -m adb_harness.connect)
fi
log=${ADB_WATCH_LOG:-$out/adb-watch.log}
interval=${ADB_WATCH_INTERVAL:-15}
pidfile=$out/adb-watch.pid

say() {
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$log"
}

# The first serial adb reports in the `device` state, or nothing.
attached() {
    adb devices 2>/dev/null | awk 'NR > 1 && $2 == "device" { print $1; exit }'
}

if [ "${1:-}" = "--status" ]; then
    pid=$(cat "$pidfile" 2>/dev/null || true)
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        echo "adb-watch running (pid $pid)"
        adb devices 2>/dev/null | awk 'NR > 1 && $2 == "device" { print "device: " $1; found = 1 } END { if (!found) print "device: none" }'
        exit 0
    fi
    echo "adb-watch not running"
    exit 1
fi

if [ -f "$pidfile" ]; then
    old=$(cat "$pidfile" 2>/dev/null)
    if [ -n "$old" ] && kill -0 "$old" 2>/dev/null; then
        echo "adb-watch is already running as pid $old" >&2
        exit 0
    fi
fi
printf '%s\n' "$$" >"$pidfile"
# Stopping the watcher is `kill "$(cat out/adb-watch.pid)"`, so the signals have
# to end the loop: a handler that only removed the pidfile left the loop running
# and writing the file again on its next pass.
cleanup() { rm -f "$pidfile"; }
trap cleanup EXIT
trap 'exit 0' INT TERM

say "watcher started (pid $$, every ${interval}s)"
state=""
failure=""
failed_at=0

while true; do
    adb start-server >/dev/null 2>&1 || true
    serial=$(attached)
    if [ -n "$serial" ]; then
        if [ "$state" != "connected:$serial" ]; then
            say "connected: $serial"
            state="connected:$serial"
        fi
        failure=""
    else
        state=""
        output=$(timeout 30 "${connect[@]}" 2>&1)
        status=$?
        now=$(date +%s)
        if [ "$status" -eq 0 ]; then
            found=$(printf '%s\n' "$output" | sed -n 's/^connected to //p' | head -1)
            say "reconnected: ${found:-$(attached)}"
            failure=""
        else
            brief=$(printf '%s\n' "$output" | grep -v '^$' | tail -2 | tr '\n' ' ')
            if [ "$brief" != "$failure" ] || [ $((now - failed_at)) -ge 600 ]; then
                say "not connected: $brief"
                failure=$brief
                failed_at=$now
            fi
        fi
    fi
    # In the background, so a kill is acted on now rather than when the sleep
    # ends: bash runs a trap only once the foreground command has returned.
    sleep "$interval" &
    wait $! 2>/dev/null || true
done
