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
#   adb-watch --status --json        # the same, as one JSON object
#   adb-watch --json                 # one JSON object per state change
#   adb-watch --interval 5 --max-attempts 10
#   kill "$(cat out/adb-watch.pid)"  # stop a running watcher
#
# From a checkout, `python3 -m adb_harness.watch` does the same.
#
# Environment: ADB_WATCH_INTERVAL (seconds, default 15), ADB_WATCH_OUT (the
# directory holding the pid, attempts and log, default out/ in the working
# directory) and ADB_WATCH_LOG (default `$ADB_WATCH_OUT/adb-watch.log`).
# `--interval` and `--max-attempts` override the interval and bound the number
# of reconnect attempts (`0`, the default, is no bound). State changes are
# logged; a repeated failure is logged again only when the message changes or ten
# minutes have passed, so a phone left with Wireless debugging off does not fill
# the log. `--quiet` leaves the log alone; JSON objects still go to stdout.
set -u

json=0
status=0
quiet=0
interval=${ADB_WATCH_INTERVAL:-15}
max_attempts=0

while [ $# -gt 0 ]; do
    case $1 in
        --status) status=1 ;;
        --json) json=1 ;;
        --quiet) quiet=1 ;;
        --interval)
            [ $# -ge 2 ] || { echo "adb-watch: --interval needs a value" >&2; exit 2; }
            interval=$2
            shift
            ;;
        --interval=*) interval=${1#*=} ;;
        --max-attempts)
            [ $# -ge 2 ] || { echo "adb-watch: --max-attempts needs a value" >&2; exit 2; }
            max_attempts=$2
            shift
            ;;
        --max-attempts=*) max_attempts=${1#*=} ;;
        *)
            echo "adb-watch: unknown option: $1" >&2
            exit 2
            ;;
    esac
    shift
done

case $interval in
    ''|*[!0-9.]*) echo "adb-watch: --interval wants seconds" >&2; exit 2 ;;
esac
case $max_attempts in
    ''|*[!0-9]*) echo "adb-watch: --max-attempts wants a whole number" >&2; exit 2 ;;
esac

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
pidfile=$out/adb-watch.pid
attempts_file=$out/adb-watch.attempts

say() {
    [ "$quiet" -eq 1 ] && return 0
    printf '%s %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >>"$log"
}

# The serials adb reports in the `device` state, one per line.
devices() {
    adb devices 2>/dev/null | awk 'NR > 1 && $2 == "device" { print $1 }'
}

# The first serial adb reports in the `device` state, or nothing.
attached() {
    devices | head -1
}

attempts() {
    local count
    count=$(cat "$attempts_file" 2>/dev/null || true)
    case $count in
        ''|*[!0-9]*) count=0 ;;
    esac
    printf '%s' "$count"
}

# JSON is built by python, so a detail line with a quote or newline stays valid.
json_status() {
    "${ADB_HARNESS_PYTHON:-python3}" -c '
import json, sys
devices = [d for d in sys.argv[3].split() if d]
print(json.dumps({"schema": "adb-watch", "version": 1,
                  "running": sys.argv[1] == "1",
                  "pid": int(sys.argv[2]) if sys.argv[2] else None,
                  "devices": devices, "attempts": int(sys.argv[4] or 0)},
                 separators=(",", ":")))
' "$@"
}

json_event() {
    "${ADB_HARNESS_PYTHON:-python3}" -c '
import json, sys
event, serial, detail, attempts = sys.argv[1:5]
devices = [d for d in sys.argv[5].split() if d]
obj = {"schema": "adb-watch", "version": 1, "event": event,
       "devices": devices, "attempts": int(attempts or 0)}
if serial:
    obj["serial"] = serial
if detail:
    obj["detail"] = detail
print(json.dumps(obj, separators=(",", ":")))
' "$@"
}

if [ "$status" -eq 1 ]; then
    pid=$(cat "$pidfile" 2>/dev/null || true)
    running=0
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
        running=1
    fi
    if [ "$json" -eq 1 ]; then
        # A watcher that is not running has no pid or attempts worth reporting.
        count=$(attempts)
        if [ "$running" -ne 1 ]; then
            pid=""
            count=0
        fi
        json_status "$running" "$pid" "$(devices | tr '\n' ' ')" "$count"
        [ "$running" -eq 1 ] && exit 0 || exit 1
    fi
    if [ "$running" -eq 1 ]; then
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
printf '0\n' >"$attempts_file"
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
count=0

while true; do
    adb start-server >/dev/null 2>&1 || true
    serial=$(attached)
    if [ -n "$serial" ]; then
        if [ "$state" != "connected:$serial" ]; then
            say "connected: $serial"
            state="connected:$serial"
            [ "$json" -eq 1 ] && json_event connected "$serial" "" "$(attempts)" "$(devices | tr '\n' ' ')"
        fi
        failure=""
    else
        if [ -n "$state" ]; then
            state=""
            [ "$json" -eq 1 ] && json_event disconnected "" "device list is empty" "$(attempts)" "$(devices | tr '\n' ' ')"
        fi
        count=$((count + 1))
        printf '%s\n' "$count" >"$attempts_file"
        output=$(timeout 30 "${connect[@]}" 2>&1)
        linked=$?
        now=$(date +%s)
        if [ "$linked" -eq 0 ]; then
            found=$(printf '%s\n' "$output" | sed -n 's/^connected to //p' | head -1)
            say "reconnected: ${found:-$(attached)}"
            [ "$json" -eq 1 ] && json_event reconnected "${found:-$(attached)}" "" "$count" "$(devices | tr '\n' ' ')"
            failure=""
        else
            brief=$(printf '%s\n' "$output" | grep -v '^$' | tail -2 | tr '\n' ' ')
            if [ "$brief" != "$failure" ] || [ $((now - failed_at)) -ge 600 ]; then
                say "not connected: $brief"
                [ "$json" -eq 1 ] && json_event error "" "$brief" "$count" "$(devices | tr '\n' ' ')"
                failure=$brief
                failed_at=$now
            fi
        fi
    fi
    if [ "$max_attempts" -gt 0 ] && [ "$count" -ge "$max_attempts" ]; then
        [ -n "$(attached)" ] && exit 0 || exit 1
    fi
    # In the background, so a kill is acted on now rather than when the sleep
    # ends: bash runs a trap only once the foreground command has returned.
    sleep "$interval" &
    wait $! 2>/dev/null || true
done
