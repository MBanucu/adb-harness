#!/usr/bin/env python3
"""A screen recording of a run, and the moments in it.

The suite records the device's screen once, from the first check to the last, and
writes an event next to every moment worth finding later: a check starting, a
key typed, a frame asked for. Nothing is extracted while the tests run. This
script is the recording, the events, and the "extract that frame now" part.

    capture start --dir out/capture
    capture event session:typed
    capture stop
    capture frames --event session:typed --out out/frames
    capture frames --at 12.5,40 --out out/frames
    capture show

A test suite can skip the shell and keep the recording in process:

    from adb_harness import capture

    with capture.Session(dir="out/capture", serial=serial) as run:
        run.event("check:diag")       # a file append: no process, no adb call
        frames = run.stop()           # Recording(video, sidecar, events)
        frame = frames.frame_for("check:diag")

`Session.event` appends a line; `Session.stop` stops the recorder, maps the
events to frames, writes the sidecar, and raises `CaptureFailed` when a step
fails rather than exiting the process. ffmpeg is only needed to extract frames
with `capture frames`, not to record or map them.

Two ways to record, one mapping
-------------------------------
The recording is screenstream's H.264 stream when it can be had: `start` brings up
the app, subscribes to its encoded port, and `stop` saves what arrived as a
fragmented MP4. That stream carries the device clock's reading for its track's
zero in its init segment, so a frame's time is the container's own pts plus that
base - the same clock the raw stream and `/proc/uptime` use. `--source stream`
insists on it; `--source auto` (the default) falls back to `screenrecord` when the
app is not installed, cannot get the projection, or never starts, printing why.

The fallback is the device's own screenrecord, timed by its Winscope metadata
track - the same clock, a different place to read it from. Everything after
`stop` is the same for both: a frame time in device nanoseconds per frame, then
the last frame at or before each event.

Why the mapping is exact, and not "the video starts when I pressed start"
-----------------------------------------------------------------------
Both sources time every frame on the device's elapsed-realtime clock: the same
clock `/proc/uptime` reports, so a moment logged on the device and a frame in the
video are comparable numbers rather than two clocks to be eyeballed at each other.
`screenrecord` writes those times into a metadata track beside the video (
`writeWinscopeMetadata` in frameworks/av's screenrecord.cpp: a magic string, the
frame count, and one 64-bit timestamp per frame), while screenstream writes the
reading for the track's zero once, in its init segment, and lets the container's
pts say the rest. Either way this script ends up with one timestamp per frame.

It pairs those with the container's own pts, so an event becomes a frame index:
the last frame presented at or before the event. An event therefore maps to the
picture that was on screen at that moment, which is what a screenshot means. No
frame rate is assumed; both sources record a frame only when the screen changes,
and gaps in time are gaps in the list.

The one number that is not from the video is the offset between the device's
clock and this machine's: the suite logs `event` on this side, the frames are
timed on that side. It is sampled twice, half a round trip each way, and the
spread between the samples is reported as the accuracy of the mapping. A round
trip is tens of milliseconds; a frame is 33 ms at 30 fps, and the sidecar says
which of the two dominated. An event that already carries a device time
(`--data device_ns=...`, as the pixel watch's do) skips the offset altogether:
its frame is on the recording's own clock, and `timed: device` says so.

Where the device predates the metadata track (Android 12 and older screenrecord),
`stop` falls back to the offset from the recording's own start and says so; the
mapping is then only as good as that assumption, and `accuracy` names it.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field

from . import __version__
from .device import adb, device_shell, host_clock, sample_clock  # noqa: F401
from .errors import CaptureFailed, NoAdb, NotConnected, SourceUnavailable  # noqa: F401
from .mp4 import MAGIC_V1, MAGIC_V2, boxes, container_pts, parse_metadata  # noqa: F401
from .output import EXIT_FAILURE, EXIT_PREREQUISITE, EXIT_USAGE, emit
from .sources import (BASE_UUID, REMOTE, STRATEGIES, RECORDING, STREAM_PORT,  # noqa: F401
                      Screenrecord, Stream, cmd_read, stream_base)

STATE = "capture.json"
EVENTS = "events.jsonl"
SIDECAR = "recording.json"
SCHEMA_START = "capture-start"
SCHEMA_RECORDING = "capture-recording"
VERSION = "capture %s" % __version__


def wants_json(args):
    """Only a parsed `--json`, not any truthy attribute a test's Mock carries."""
    return getattr(args, "json", False) is True


def quiet(args):
    return getattr(args, "quiet", False) is True


def fail(args, schema, reason, message, code):
    if wants_json(args):
        emit(schema, ok=False, reason=reason, detail=message)
    else:
        print(message, file=sys.stderr)
    return code


def load_state(directory):
    path = os.path.join(directory, STATE)
    if not os.path.isfile(path):
        raise CaptureFailed("no recording in %s (run start first)" % directory)
    with open(path) as handle:
        return json.load(handle)


def save_state(directory, state):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, STATE), "w") as handle:
        json.dump(state, handle, indent=2)


def append_event(directory, name, data=None):
    """One event line. A file append and nothing else: no process, no adb."""
    record = {"name": name, "host": host_clock()}
    if data:
        record["data"] = {str(key): str(value) for key, value in data.items()}
    with open(os.path.join(directory, EVENTS), "a") as handle:
        handle.write(json.dumps(record) + "\n")


@dataclass(frozen=True)
class Recording:
    """What `Session.stop` produced.

    `video` and `sidecar` are paths; `events` are the mapped event objects (the
    sidecar's `events`), and `data` is the sidecar as a dict. `frame_for(name)`
    is the first mapped event of that name, or None when nothing of that name was
    marked (or it could not be mapped).
    """

    video: str
    sidecar: str
    events: tuple = ()
    data: dict = field(default_factory=dict)

    def frame_for(self, name):
        """The first mapped event called `name`, or None."""
        for event in self.events:
            if event.get("name") == name and "frame" in event:
                return event
        return None

    def frames_for(self, name):
        """Every mapped event called `name`; a repeated name keeps every moment."""
        return tuple(event for event in self.events
                     if event.get("name") == name and "frame" in event)


class Session:
    """One recording, owned from start to stop.

    `Session(dir=..., serial=...)` records the named device; the serial is what
    makes a machine with two attached devices unambiguous. Use it as a context
    manager and it stops itself, or call `stop()` for the `Recording`:

        with capture.Session(dir="out/capture", serial="127.0.0.1:38109") as run:
            run.event("check:diag")
            frames = run.stop()
            frame = frames.frame_for("check:diag")

    The library path never prints. `on_note` receives the progress lines the
    commands print (the fallback reason, a container/metadata frame-count
    mismatch); with no callback they are dropped.
    """

    def __init__(self, dir="out/capture", serial="", *, source="auto", size="",
                 bit_rate="6M", apk="", on_note=None):
        self.dir = dir
        self.serial = serial
        self.source = source
        self.size = size
        self.bit_rate = bit_rate
        self.apk = apk
        self.on_note = on_note
        self.state = {}
        self._started = False
        self._recording = None

    def _note(self, message):
        if self.on_note is not None:
            self.on_note(message)

    @staticmethod
    def _require_adb():
        if shutil.which(os.environ.get("ADB", "adb")) is None:
            raise NoAdb("adb is not in PATH")

    def start(self):
        """Starts the recording and writes the state and the start event."""
        self._require_adb()
        os.makedirs(self.dir, exist_ok=True)
        self.serial = self.serial or os.environ.get("ANDROID_SERIAL", "")
        if adb(["get-state"], self.serial).returncode != 0:
            raise NotConnected("no device (set ANDROID_SERIAL or --serial)")
        clock = sample_clock(self.serial)
        if clock is None:
            raise CaptureFailed("could not read the device clock")
        chosen = self.source
        if chosen == "auto":
            # The stream is one encoder for a viewer, so it costs less than
            # screenrecord on the device; when it cannot run, this is the
            # recording that always can.
            try:
                extra = Stream().start(self, self.serial)
                chosen = "stream"
            except SourceUnavailable as why:
                self._note("capture: %s; falling back to screenrecord" % why)
                chosen = "screenrecord"
                try:
                    extra = Screenrecord().start(self, self.serial)
                except SourceUnavailable as failed:
                    raise CaptureFailed(str(failed)) from None
        else:
            try:
                extra = STRATEGIES[chosen]().start(self, self.serial)
            except SourceUnavailable as why:
                raise CaptureFailed(str(why)) from None
        self.source = chosen
        state = {"source": chosen, "serial": self.serial, "started": time.time(),
                 "host_clock": host_clock(), "clock": [clock]}
        state.update(extra)
        save_state(self.dir, state)
        with open(os.path.join(self.dir, EVENTS), "w") as handle:
            handle.write(json.dumps({"name": "record:start", "host": host_clock()}) + "\n")
        self.state = state
        self._started = True
        return self

    def event(self, name, data=None):
        """Marks a moment. A file append only: no process and no adb call."""
        if not self._started and not os.path.isfile(
                os.path.join(self.dir, STATE)):
            raise CaptureFailed("no recording in %s (run start first)" % self.dir)
        append_event(self.dir, name, data)
        return self

    def stop(self, serial=None):
        """Stops, pulls, maps, writes the sidecar, and returns the `Recording`."""
        if self._recording is not None:
            return self._recording
        self._require_adb()
        state = dict(self.state) if self.state else load_state(self.dir)
        serial = serial or self.serial or state.get("serial", "")
        stop_sampled = sample_clock(serial)
        name = state.get("source", "screenrecord")
        strategy = STRATEGIES.get(name, Screenrecord)()
        try:
            recording, meta, extra = strategy.stop(self, state, serial)
        except SystemExit as stopped:
            message = stopped.code if isinstance(stopped.code, str) else "capture failed"
            raise CaptureFailed(message) from None
        sidecar = self._finish(recording, state, meta, stop_sampled, extra)
        self._recording = Recording(
            video=recording,
            sidecar=os.path.join(self.dir, SIDECAR),
            events=tuple(sidecar.get("events") or ()),
            data=sidecar)
        return self._recording

    def _finish(self, recording, state, meta, stop_sampled, extra=None):
        """The sidecar both strategies end with: frame times, then the events on them."""
        pts = container_pts(recording)
        sidecar = {"recording": os.path.basename(recording),
                   "source": state.get("source", "screenrecord"),
                   "size": os.path.getsize(recording),
                   "duration": pts[-1] - pts[0] if len(pts) > 1 else 0.0,
                   "events": [], "frames": []}
        if state.get("clock") and stop_sampled:
            state["clock"].append(stop_sampled)
        if meta is None:
            sidecar["sync"] = {"clock": "none", "accuracy_ms": None,
                               "why": (extra or {}).get("why")
                                      or "this recording has no per-frame times"}
        else:
            sidecar["frames"] = meta["frames"]
            sidecar["sync"] = {"clock": meta["clock"], "unit_ns": int(meta["unit"]),
                               "frame_count": len(meta["frames"]),
                               "epoch_offset_ns": meta.get("epoch_offset_ns")}
            if meta.get("base_ns") is not None:
                # A stream's frames are its own pts plus this base, so the file that
                # produced them is in the sidecar too: the mapping explains itself.
                sidecar["sync"]["base_ns"] = meta["base_ns"]
            if extra:
                sidecar["sync"].update(extra)
            clocks = state.get("clock") or []
            if clocks:
                offsets = [sample["offset"] for sample in clocks]
                errors = [sample["error"] for sample in clocks]
                sidecar["sync"]["offset"] = sum(offsets) / len(offsets)
                sidecar["sync"]["offset_samples"] = len(offsets)
                # The spread of the samples is the honest error of the offset; half a
                # round trip is the floor. A frame at 30 fps is 33 ms, so this says
                # whether the mapping is frame accurate or only approximate.
                sidecar["sync"]["offset_spread_ms"] = 1000.0 * (
                    max(offsets) - min(offsets)) / 2.0
                sidecar["sync"]["round_trip_ms"] = 1000.0 * sum(errors) / len(errors)
            if pts and len(pts) != len(meta["frames"]):
                # A count that differs means an index would point at the wrong
                # picture; the timestamps are still good for --at.
                self._note("capture: %d frames in the container, %d in the metadata: "
                           "events keep their frame index, and --at still works"
                           % (len(pts), len(meta["frames"])))
        sidecar["events"] = map_events(self.dir, sidecar)
        with open(os.path.join(self.dir, SIDECAR), "w") as handle:
            json.dump(sidecar, handle, indent=2)
        return sidecar

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, tb):
        if self._recording is not None:
            return False
        if exc_type is None:
            self.stop()
        else:
            # Do not leave a recorder behind, but keep the exception that was
            # already travelling to the caller.
            try:
                self.stop()
            except Exception:
                pass
        return False


def cmd_start(args):
    session = Session(dir=args.dir, serial=getattr(args, "serial", ""),
                      source=getattr(args, "source", "auto"),
                      size=getattr(args, "size", ""),
                      bit_rate=getattr(args, "bit_rate", "6M"),
                      apk=getattr(args, "apk", ""),
                      on_note=None if (wants_json(args) or quiet(args)) else print)
    try:
        session.start()
    except NoAdb as why:
        return fail(args, SCHEMA_START, "no-adb", "capture: %s" % why,
                    EXIT_PREREQUISITE)
    except NotConnected as why:
        return fail(args, SCHEMA_START, "no-device", "capture: %s" % why,
                    EXIT_FAILURE)
    except CaptureFailed as why:
        return fail(args, SCHEMA_START, "record-failed", "capture: %s" % why,
                    EXIT_FAILURE)
    if wants_json(args):
        emit(SCHEMA_START, recording=True, source=session.source, dir=args.dir,
             serial=session.serial)
    else:
        print("capture: %s recording, events in %s"
              % (session.source, os.path.join(args.dir, EVENTS)))
    return 0


def cmd_event(args):
    directory = args.dir
    if not os.path.isdir(directory):
        return 0
    if not os.path.isfile(os.path.join(directory, STATE)):
        return 0
    data = {}
    for pair in args.data or []:
        key, _, value = pair.partition("=")
        data[key.strip()] = value.strip()
    append_event(directory, args.name, data)
    return 0


def cmd_stop(args):
    session = Session(dir=args.dir, serial=getattr(args, "serial", ""),
                      on_note=print)
    try:
        recording = session.stop()
    except NoAdb as why:
        print("capture: %s" % why, file=sys.stderr)
        return EXIT_PREREQUISITE
    except CaptureFailed as why:
        print("capture: %s" % why, file=sys.stderr)
        return EXIT_FAILURE
    print_summary(args.dir, recording.data)
    return 0


def finish(args, state, recording, meta, stop_sampled, extra=None):
    """Kept for callers of the old internal helper; the CLI uses `Session`."""
    session = Session(dir=args.dir, on_note=print)
    sidecar = session._finish(recording, state, meta, stop_sampled, extra)
    print_summary(args.dir, sidecar)
    return 0


def map_events(directory, sidecar):
    frames = sidecar.get("frames") or []
    sync = sidecar.get("sync") or {}
    pts = container_pts(os.path.join(directory, RECORDING))
    out = []
    path = os.path.join(directory, EVENTS)
    if not os.path.isfile(path):
        return out
    with open(path, errors="replace") as handle:
        lines = handle.read().splitlines()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        mapped = {"name": event.get("name"), "host": event.get("host")}
        data = event.get("data") or {}
        if data:
            mapped["data"] = data
        device_ns = None
        from_host = False
        if data.get("device_ns"):
            # A moment the device timed itself is already on the clock the frames
            # are timed by, so it needs no offset and is not made worse by one.
            # The pixel watch is this case: it passes on a frame of the screen it
            # took, which the recording has its own timestamp for.
            try:
                device_ns = float(data["device_ns"])
            except (TypeError, ValueError):
                device_ns = None
        if device_ns is None and frames and "offset" in sync:
            device_ns = (event["host"] + sync["offset"]) * 1e9
            from_host = True
        if frames and device_ns is not None:
            mapped["timed"] = "host" if from_host else "device"
            at = 0
            for index, stamp in enumerate(frames):
                if stamp <= device_ns:
                    at = index
                else:
                    break
            mapped["frame"] = at
            # How long before the event that frame had appeared. A still screen
            # produces no new frames, so this is often seconds and is not an
            # error: that frame was what was on screen. Negative means the event
            # came before anything had been drawn, which only the start event of
            # a run does.
            mapped["since_frame_ms"] = round((device_ns - frames[at]) / 1e6, 1)
            if device_ns < frames[0]:
                mapped["before_video"] = True
            if at < len(pts):
                mapped["video_s"] = round(pts[at], 3)
            elif pts:
                # The container lists fewer frames than the metadata does: the
                # last time it knows is extended by the gap the metadata reports.
                mapped["video_s"] = round(pts[-1] + (device_ns - frames[-1]) / 1e9, 3)
            # With no container times at all (no ffprobe, or a file it cannot
            # read) there is no presentation time to report, and the frame index
            # and the time since that frame are still the answer.
        out.append(mapped)
    return out


def print_summary(directory, sidecar):
    sync = sidecar.get("sync") or {}
    frames = sidecar.get("frames") or []
    events = sidecar.get("events") or []
    print("capture: %s  %.1f s  %.1f MB  %d frame(s)"
          % (sidecar["recording"], sidecar["duration"],
             sidecar["size"] / 1e6, len(frames)))
    if sync.get("clock") == "none":
        print("capture: no per-frame timestamps: events are not mapped")
        return
    period = 0.0
    if len(frames) > 1:
        gaps = sorted((b - a) / sync["unit_ns"] for a, b in zip(frames, frames[1:]))
        period = gaps[len(gaps) // 2]
    print("capture: clock %s%s, events mapped %d/%d, typical frame %.0f ms, "
          "clock offset error %.0f ms (%.1f ms round trip)"
          % (sync["clock"], " (per-frame)" if frames else "",
             sum(1 for e in events if "frame" in e), len(events), period * 1000,
             sync.get("offset_spread_ms") or 0.0, sync.get("round_trip_ms") or 0.0))


def cmd_show(args):
    directory = args.dir
    sidecar_path = os.path.join(directory, SIDECAR)
    if not os.path.isfile(sidecar_path):
        return fail(args, SCHEMA_RECORDING, "no-recording",
                    "capture: no recording.json in %s (run stop first)" % directory,
                    EXIT_FAILURE)
    with open(sidecar_path) as handle:
        sidecar = json.load(handle)
    if wants_json(args):
        # The sidecar as it stands, so `capture frames --sidecar` accepts this
        # written back to a file: one format, with the schema added to it.
        emit(SCHEMA_RECORDING, **sidecar)
        return 0
    print_summary(directory, sidecar)
    for event in sidecar.get("events") or []:
        if "frame" not in event:
            print("  %-40s (not mapped)" % event["name"])
            continue
        print("  %-40s frame %4d  at %7.3fs  %6.0f ms after that frame"
              % (event["name"], event["frame"], event["video_s"],
                 event["since_frame_ms"]))
    return 0


def frame_command(recording, video_s, out):
    # Seek just before the frame's own pts: ffmpeg seeks to a keyframe and decodes
    # forward, and `-frames:v 1` then hands over the first frame at or after the
    # seek, which is the frame that was on screen.
    return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-ss", "%.6f" % max(0.0, video_s - 0.001), "-i", recording,
            "-map", "0:v:0", "-frames:v", "1", out]


def cmd_frames(args):
    if shutil.which("ffmpeg") is None:
        print("capture: ffmpeg is not in PATH", file=sys.stderr)
        return EXIT_PREREQUISITE
    directory = args.dir
    sidecar_path = args.sidecar or os.path.join(directory, SIDECAR)
    if not os.path.isfile(sidecar_path):
        print("capture: no recording.json; run stop first", file=sys.stderr)
        return EXIT_FAILURE
    with open(sidecar_path) as handle:
        sidecar = json.load(handle)
    recording = args.recording or os.path.join(directory, sidecar["recording"])
    if not os.path.isfile(recording):
        print("capture: no recording at %s" % recording, file=sys.stderr)
        return EXIT_FAILURE
    events = sidecar.get("events") or []
    wanted = [name.strip() for name in (args.event or "").split(",") if name.strip()]
    missing = [name for name in wanted
               if not any(e["name"] == name and "video_s" in e for e in events)]
    if missing:
        print("capture: no mapped event named: " + ", ".join(missing), file=sys.stderr)
        return EXIT_USAGE
    picks = []
    if args.all:
        picks = [e for e in events if "video_s" in e]
    for name in wanted:
        # A repeated name (a check that marks its own start and end with the same
        # name) gives every moment, not one of them.
        picks.extend([e for e in events if e["name"] == name and "video_s" in e])
    for value in (args.at or "").split(","):
        value = value.strip()
        if value:
            try:
                picks.append({"name": "at-%s" % value, "video_s": float(value)})
            except ValueError:
                print("capture: --at wants seconds, not %r" % value, file=sys.stderr)
                return EXIT_USAGE
    if not picks:
        print("capture: say what to extract: --event NAME, --at SECONDS, or --all",
              file=sys.stderr)
        return EXIT_USAGE
    os.makedirs(args.out, exist_ok=True)
    for pick in picks:
        slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", pick["name"]).strip("-")
        out = os.path.join(args.out, "%07.3fs-%s.png" % (pick["video_s"], slug))
        result = subprocess.run(frame_command(recording, pick["video_s"], out),
                                capture_output=True, text=True)
        if result.returncode != 0:
            print("capture: ffmpeg failed at %.3fs: %s"
                  % (pick["video_s"], result.stderr.strip()), file=sys.stderr)
            return EXIT_FAILURE
        print("%s  %.3fs  %s" % (out, pick["video_s"], pick.get("name", "")))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(
        description="Record a run once, keep the moments as events, extract "
                    "frames when asked.")
    parser.add_argument("--version", action="version", version=VERSION)
    parser.add_argument("--dir", default="out/capture",
                        help="where the recording and its events live")
    parser.add_argument("--serial", default="",
                        help="serial to use when more than one device is attached")
    sub = parser.add_subparsers(dest="command", required=True)

    def with_dir(parser, **kwargs):
        """Every subcommand takes --dir and --serial: the shell writes them after
        the verb, and naming a serial makes two attached devices unambiguous."""
        parser.add_argument("--dir", dest="sub_dir", default="")
        parser.add_argument("--serial", dest="sub_serial", default="")
        parser.add_argument("--version", action="version", version=VERSION)
        return parser

    start = with_dir(sub.add_parser("start", help="start recording the device's screen"))
    start.add_argument("--size", default="", help="WxH; native by default")
    start.add_argument("--bit-rate", dest="bit_rate", default="6M")
    # screenstream is the recording to prefer: one hardware encode, times on the
    # device's clock, and no second process competing for the display. screenrecord
    # is what records when it cannot run, and neither is required by the other.
    start.add_argument("--source", choices=("auto", "stream", "screenrecord"),
                       default="auto",
                       help="how to record: screenstream, falling back to "
                            "screenrecord (default); stream; screenrecord")
    start.add_argument("--apk", default="",
                       help="APK to install when the screenstream app is missing "
                            "(default: the latest GitHub release; CAPTURE_APK also "
                            "works)")
    start.add_argument("--json", action="store_true",
                       help="print one JSON object on stdout instead of prose")
    start.add_argument("--quiet", action="store_true",
                       help="no progress lines; the final output is still printed")
    start.set_defaults(run=cmd_start)

    event = with_dir(sub.add_parser("event", help="mark a moment; no adb call"))
    event.add_argument("name")
    event.add_argument("--data", action="append", metavar="KEY=VALUE")
    event.set_defaults(run=cmd_event)

    stop = with_dir(sub.add_parser("stop", help="stop, pull, and map the events to frames"))
    stop.set_defaults(run=cmd_stop)

    show = with_dir(sub.add_parser("show", help="the mapped events of a recording"))
    show.add_argument("--json", action="store_true",
                      help="print the sidecar as one JSON object, with a schema")
    show.set_defaults(run=cmd_show)

    frames = with_dir(sub.add_parser("frames", help="extract frames, now"))
    frames.add_argument("--event", default="", help="event name(s), comma separated")
    frames.add_argument("--at", default="", help="seconds into the video, comma separated")
    frames.add_argument("--all", action="store_true", help="every mapped event")
    frames.add_argument("--out", default="out/frames")
    frames.add_argument("--recording", default="")
    frames.add_argument("--sidecar", default="")
    frames.set_defaults(run=cmd_frames)

    # The stream subscriber, started by `start` and stopped by `stop`.
    read = with_dir(sub.add_parser(
        "read", help="write the stream to the directory; start runs this"))
    read.add_argument("--host", default="127.0.0.1")
    read.add_argument("--port", type=int, default=STREAM_PORT)
    read.set_defaults(run=cmd_read)

    return parser


def main(argv=None):
    args = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    # `--dir` and `--serial` are accepted on either side of the verb; the shell
    # writes them after.
    args.dir = args.sub_dir or args.dir
    args.serial = args.sub_serial or args.serial
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
