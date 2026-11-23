"""The two ways to record the screen, and what each says about frame times.

`screenrecord` writes a metadata track beside its video; screenstream writes the
device clock's reading for its track's zero in the init segment of a fragmented
MP4. Either way a frame ends up with a device time, which is what the mapping in
capture.py needs.
"""
import os
import signal
import socket
import struct
import subprocess
import sys
import time

from .device import adb, device_shell
from .mp4 import boxes, container_pts, parse_metadata

RECORDING = "recording.mp4"
REMOTE = "/data/local/tmp/suite-capture.mp4"

# screenstream: the second way to record. Its encoded stream carries the device
# clock's reading for the track's zero, so its frames are timed without a track.
PACKAGE = "io.github.mbanucu.screenstream.app"
ACTIVITY = PACKAGE + "/io.github.mbanucu.screenstream.ScreenStreamActivity"
STATUS = "/sdcard/Android/data/%s/files/screenstream.txt" % PACKAGE
STREAM_PORT = 7101
BASE_UUID = bytes.fromhex("6d42414e7563750073747265616d0001")


def device_status(serial=""):
    """The screenstream app's status file as a dict, or nothing when it is absent."""
    result = device_shell("cat %s" % STATUS, serial)
    if result.returncode != 0:
        return {}
    state = {}
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key.strip():
            state[key.strip()] = value.strip()
    return state


def stream_base(path):
    """The device time of the stream's track zero, from its init segment, or None.

    screenstream writes the device clock's reading for the first frame in a private
    `udta`/`uuid` box: a track's times count from that frame, so without this the
    file says how far apart its frames were but not when any of them happened.
    """
    with open(path, "rb") as handle:
        data = handle.read(1 << 16)
    for kind, pos, header, size in boxes(data, 0, len(data)):
        if kind != b"moov":
            continue
        for kind2, pos2, header2, size2 in boxes(data, pos + header, pos + size):
            if kind2 != b"udta":
                continue
            for kind3, pos3, header3, size3 in boxes(data, pos2 + header2,
                                                     pos2 + size2):
                body = pos3 + header3
                if kind3 != b"uuid" or size3 - header3 < 32:
                    continue
                if data[body:body + 16] != BASE_UUID:
                    continue
                if struct.unpack(">I", data[body + 16:body + 20])[0] != 1:
                    continue
                return struct.unpack(">Q", data[body + 24:body + 32])[0]
    return None


class SourceUnavailable(Exception):
    """A way of recording cannot run here; `auto` falls back to screenrecord."""


class Screenrecord:
    """The device's own screenrecord, whose metadata track times every frame."""

    name = "screenrecord"

    def start(self, args, serial):
        device_shell("rm -f %s" % REMOTE, serial)
        # A leftover recorder from a dead run would hold the display's capture slot
        # and would also be found by stop, so it is stopped before this one starts.
        device_shell("pkill -INT screenrecord", serial)
        option = ""
        if args.size:
            option += " --size %s" % args.size
        if args.bit_rate:
            option += " --bit-rate %s" % args.bit_rate
        # setsid + & so the recorder outlives this adb call: start and stop are
        # separate commands, and the suite does not hold a client open for a run.
        device_shell("setsid screenrecord --time-limit 0%s %s </dev/null >/dev/null "
                     "2>&1 &" % (option, REMOTE), serial)
        for _ in range(40):
            if device_shell("pidof screenrecord", serial).returncode == 0:
                return {"remote": REMOTE, "size": args.size or "native",
                        "bit_rate": args.bit_rate or "default"}
            time.sleep(0.05)
        raise SourceUnavailable("screenrecord did not start")

    def stop(self, args, state, serial):
        device_shell("pkill -INT screenrecord", serial)
        for _ in range(100):
            if device_shell("pidof screenrecord", serial).returncode != 0:
                break
            time.sleep(0.05)
        else:
            sys.exit("capture: screenrecord would not stop")
        recording = os.path.join(args.dir, RECORDING)
        pull = adb(["pull", state["remote"], os.path.abspath(recording)], serial)
        if pull.returncode != 0:
            sys.exit("capture: could not pull the recording: %s" % pull.stderr.strip())
        device_shell("rm -f %s" % state["remote"], serial)
        # The file screenrecord closes is used as it is. It already keeps `moov`
        # before `mdat`, so seeking it is a seek, and its metadata track carries the
        # per-frame times: remuxing it through ffmpeg would drop that track and make
        # the recording stop explaining itself.
        return recording, parse_metadata(recording), None


class Stream:
    """screenstream's H.264 stream on port 7101, saved as a fragmented MP4.

    The stream carries the device clock's reading for its track's zero, so the
    frame times are the container's own pts plus that base - the same clock the raw
    stream's frame headers use, and no metadata track to keep in step.
    """

    name = "stream"

    def start(self, args, serial):
        # A second launch of the activity ends the projection the first one owns, so
        # a run begins from a stopped app rather than from whatever was left.
        device_shell("am force-stop %s" % PACKAGE, serial)
        # The projection dialog needs a hand on the screen; this app-op is the same
        # grant without one. When it cannot be set the app asks, and `auto` falls back.
        device_shell("appops set %s PROJECT_MEDIA allow" % PACKAGE, serial)
        device_shell("rm -f %s" % STATUS, serial)
        device_shell("am start -n %s" % ACTIVITY, serial)
        for _ in range(60):
            if device_status(serial).get("state") == "streaming":
                break
            time.sleep(0.1)
        else:
            raise SourceUnavailable(
                "screenstream is not streaming (installed? projection allowed?)")
        reader = subprocess.Popen(
            [sys.executable, "-m", "adb_harness.capture", "--dir",
             os.path.abspath(args.dir), "read", "--port", str(STREAM_PORT)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        for _ in range(30):
            if device_status(serial).get("encoded_clients", "0") != "0":
                return {"reader_pid": reader.pid, "port": STREAM_PORT}
            time.sleep(0.1)
        reader.terminate()
        reader.wait(timeout=5)
        device_shell("am start -n %s --ez stop true" % ACTIVITY, serial)
        raise SourceUnavailable("nothing is listening on port %d" % STREAM_PORT)

    def stop(self, args, state, serial):
        # Ending the projection closes the socket, so the reader finishes what it
        # has rather than being cut off in the middle of a fragment.
        device_shell("am start -n %s --ez stop true" % ACTIVITY, serial)
        self._reap(state.get("reader_pid"))
        for _ in range(50):
            if device_status(serial).get("state") == "stopped":
                break
            time.sleep(0.1)
        recording = os.path.join(args.dir, RECORDING)
        if not os.path.isfile(recording) or os.path.getsize(recording) == 0:
            sys.exit("capture: screenstream sent nothing; a screen that never "
                     "changes sends no frames")
        base = stream_base(recording)
        if base is None:
            return recording, None, {
                "why": "the stream's init segment carried no device time"}
        # The container's pts are relative to the track's zero, and the base is
        # where that zero was on the device's clock.
        frames = [base + int(round(point * 1e9))
                  for point in container_pts(recording)]
        return recording, {"clock": "elapsed", "unit": 1e9, "frames": frames,
                           "base_ns": base}, None

    @staticmethod
    def _reap(pid):
        """Waits for the reader to write what it has, then stops it."""
        if not pid:
            return
        for _ in range(100):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass


STRATEGIES = {"screenrecord": Screenrecord, "stream": Stream}


def cmd_read(args):
    """The stream subscriber: run in the background by `start --source stream`."""
    deadline = time.monotonic() + 5
    while True:
        try:
            link = socket.create_connection((args.host, args.port), timeout=5)
            break
        except OSError:
            if time.monotonic() > deadline:
                return 1
            time.sleep(0.2)
    os.makedirs(args.dir, exist_ok=True)
    with link, open(os.path.join(args.dir, RECORDING), "wb") as out:
        link.settimeout(1.0)
        while True:
            try:
                chunk = link.recv(1 << 16)
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            out.write(chunk)
    return 0
