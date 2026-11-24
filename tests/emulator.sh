#!/usr/bin/env bash
# Attached-emulator smoke test for the capture path.
#
# The emulator-e2e workflow runs this inside an API 35 AVD, but it is a plain
# shell script so the same sequence can be run by hand against any attached
# emulator. It refuses to pass without an emulator-* serial in the adb device
# state: a missing device is a failed run, never a skipped one.
#
#   bash tests/emulator.sh                 # full smoke test
#   bash tests/emulator.sh --check-device  # only the attached-device preflight
#
# The recording itself needs only adb and this checkout. ffprobe and ffmpeg are
# used when they already exist; this script does not install them.

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd -- "$SCRIPT_DIR/.." && pwd)"
OUT="${EMULATOR_OUT:-$ROOT/out/emulator}"
CHECK_ONLY=0
VENV=""

case "${1:-}" in
    --check-device) CHECK_ONLY=1 ;;
    "") ;;
    *)
        echo "emulator smoke: unknown argument: $1" >&2
        exit 2
        ;;
esac

die() {
    echo "emulator smoke: $*" >&2
    exit 1
}

finish() {
    local status=$?
    if [ -n "$VENV" ]; then
        rm -rf "$VENV"
    fi
    if [ "$status" -ne 0 ]; then
        echo "emulator smoke: failed (exit $status); artifacts are under $OUT" >&2
    fi
    exit "$status"
}
trap finish EXIT

cd "$ROOT"

if [ "$CHECK_ONLY" -eq 0 ]; then
    mkdir -p "$OUT"
fi

command -v adb >/dev/null 2>&1 || die "adb is not in PATH"

# The action waits for the boot, but adb can still be starting. This is one
# check, not a retry loop: an emulator that is not in the device state now is a
# failed run, not a reason to report a green one.
devices="$(adb devices)"
if [ "$CHECK_ONLY" -eq 0 ]; then
    printf '%s\n' "$devices" > "$OUT/adb-devices.txt"
fi
SERIAL="$(printf '%s\n' "$devices" | awk '$1 ~ /^emulator-[0-9]+$/ && $2 == "device" { print $1; exit }')"
[ -n "$SERIAL" ] || die "no attached emulator-* in the adb device state (adb devices showed none)"
echo "emulator smoke: attached $SERIAL"

if [ "$CHECK_ONLY" -eq 1 ]; then
    exit 0
fi

export ANDROID_SERIAL="$SERIAL"
{
    adb -s "$SERIAL" shell getprop ro.build.version.sdk
    adb -s "$SERIAL" shell getprop ro.product.cpu.abi
    adb -s "$SERIAL" shell wm size
} > "$OUT/device.txt" 2>&1 || true

# A throwaway install is what a user gets: entry points, package data and all.
VENV="$(mktemp -d "${TMPDIR:-/tmp}/adb-harness-emulator-venv.XXXXXX")"
python3 -m venv "$VENV"
"$VENV/bin/pip" install --quiet ".[mdns]"

"$VENV/bin/adb-connect" --json --serial "$SERIAL" > "$OUT/adb-connect.json"

# No watcher is expected in the job, so --status exits 1. The JSON it prints is
# still the real adb device list, which is the point of exercising it here.
set +e
"$VENV/bin/adb-watch" --status --json --serial "$SERIAL" > "$OUT/adb-watch.json"
watch_status=$?
set -e
case "$watch_status" in
    0|1) ;;
    *) die "adb-watch --status exited $watch_status" ;;
esac

"$VENV/bin/capture" start --source screenrecord --dir "$OUT" --serial "$SERIAL" \
    --json > "$OUT/capture-start.json"

# Make the screen move after the recorder is running. HOME settles a locked or
# unknown screen, the notification shade is a large visible change, and BACK
# closes it again.
adb -s "$SERIAL" shell input keyevent KEYCODE_WAKEUP
adb -s "$SERIAL" shell wm dismiss-keyguard
adb -s "$SERIAL" shell input keyevent KEYCODE_HOME
sleep 1
adb -s "$SERIAL" shell input swipe 540 0 540 1000 300
sleep 1
adb -s "$SERIAL" shell input keyevent KEYCODE_BACK
sleep 1
"$VENV/bin/capture" event emulator:visible-change --dir "$OUT" --serial "$SERIAL"
sleep 1
"$VENV/bin/capture" stop --dir "$OUT" --serial "$SERIAL" | tee "$OUT/capture-stop.txt"

# Load the command JSON and the sidecar with the standard library only. The
# MP4 must exist, be non-empty, carry per-frame metadata, and map the event to
# one of those frames. ffprobe and ffmpeg add checks when they are installed;
# they are never a prerequisite for the base path.
"$VENV/bin/python" - "$OUT" "$SERIAL" <<'PY'
import json
import os
import shutil
import subprocess
import sys

out, serial = sys.argv[1], sys.argv[2]


def load(name):
    with open(os.path.join(out, name)) as handle:
        return json.load(handle)


connect = load("adb-connect.json")
if connect.get("ok") is not True:
    raise SystemExit("adb-connect did not report ok: %r" % connect)
if connect.get("source") != "attached":
    raise SystemExit("adb-connect source is %r, not attached"
                     % connect.get("source"))
if connect.get("serial") != serial:
    raise SystemExit("adb-connect serial is %r, expected %r"
                     % (connect.get("serial"), serial))

watch = load("adb-watch.json")
if serial not in (watch.get("devices") or []):
    raise SystemExit("adb-watch --status did not list %s: %r" % (serial, watch))

start = load("capture-start.json")
if start.get("recording") is not True or start.get("source") != "screenrecord":
    raise SystemExit("capture start did not begin a screenrecord: %r" % start)
if start.get("serial") != serial:
    raise SystemExit("capture start serial is %r, expected %r"
                     % (start.get("serial"), serial))

sidecar = load("recording.json")
video = os.path.join(out, sidecar.get("recording") or "recording.mp4")
if not os.path.isfile(video):
    raise SystemExit("no recording at %s" % video)
size = os.path.getsize(video)
if size <= 0:
    raise SystemExit("recording %s is empty" % video)
if sidecar.get("size") != size:
    raise SystemExit("sidecar size %r does not match file size %d"
                     % (sidecar.get("size"), size))
if sidecar.get("source") != "screenrecord":
    raise SystemExit("sidecar source is %r, not screenrecord"
                     % sidecar.get("source"))

frames = sidecar.get("frames") or []
if not frames:
    raise SystemExit("sidecar has no per-frame metadata")

event = next((item for item in sidecar.get("events") or []
              if item.get("name") == "emulator:visible-change"), None)
if event is None:
    raise SystemExit("no emulator:visible-change event in the sidecar")
index = event.get("frame")
if not isinstance(index, int) or not 0 <= index < len(frames):
    raise SystemExit("event frame %r is not an index into %d frame(s)"
                     % (index, len(frames)))
if event.get("timed") not in ("host", "device"):
    raise SystemExit("event was not timed: %r" % event)

ffprobe = shutil.which("ffprobe")
if ffprobe:
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-count_frames",
         "-show_entries", "stream=nb_read_frames",
         "-of", "default=nw=1:nk=1", video],
        capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit("ffprobe could not read %s: %s"
                         % (video, result.stderr.strip()))
    try:
        count = int(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise SystemExit("ffprobe reported no video frame count: %r"
                         % result.stdout)
    if count <= 0:
        raise SystemExit("ffprobe counted no video frames")
    if "video_s" not in event:
        raise SystemExit("ffprobe is available but the event has no video_s")
    print("emulator smoke: ffprobe counted %d frame(s); event frame %d at %.3fs"
          % (count, index, event["video_s"]))
else:
    print("emulator smoke: ffprobe not installed; frame mapping checked from "
          "the sidecar only")

report = {
    "serial": serial,
    "source": connect["source"],
    "recording": os.path.basename(video),
    "size": size,
    "frames": len(frames),
    "event": {"name": event["name"], "frame": index,
              "video_s": event.get("video_s")},
}
with open(os.path.join(out, "report.json"), "w") as handle:
    json.dump(report, handle, indent=2)

print("emulator smoke: recording %s (%d bytes), %d frame time(s), event frame %d"
      % (os.path.basename(video), size, len(frames), index))
PY

if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
    "$VENV/bin/capture" frames --dir "$OUT" --serial "$SERIAL" \
        --event emulator:visible-change --out "$OUT/frames"
    extracted="$(find "$OUT/frames" -type f -name '*.png' -size +0c | wc -l)"
    [ "$extracted" -gt 0 ] || die "capture frames wrote no non-empty PNG"
    echo "emulator smoke: extracted $extracted frame(s) with ffmpeg"
else
    echo "emulator smoke: ffmpeg/ffprobe not installed; frame extraction skipped"
fi

echo "emulator smoke: ok"
