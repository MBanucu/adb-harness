"""Tests for capture.py's event-to-frame mapping.

The recording is built here rather than taken from a phone: a synthetic MP4 with
the same shape as `screenrecord`'s - an `ftyp`, a `moov` holding a metadata track
whose one sample is a Winscope payload, and the sample's bytes in `mdat`. That
makes the parser and the mapping testable without a device, and without ffmpeg:
`container_pts` is what reads the container's own timestamps with ffprobe, so it
is patched where the expected pts matter and left empty where they do not.
"""

import json
import os
import struct
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adb_harness import capture  # noqa: E402


# --- building a recording ----------------------------------------------------


def box(kind, *parts):
    body = b"".join(parts)
    return struct.pack(">I4s", 8 + len(body), kind) + body


FULL = b"\0\0\0\0"  # version 0, no flags


def winscope_payload(frames_ns, version=2, epoch_offset_ns=0):
    """The bytes screenrecord writes into its metadata track's one sample."""
    stamps = struct.pack("<%dQ" % len(frames_ns), *frames_ns)
    if version == 2:
        # #VV1NSC0PET1ME2#, 4 bytes of width/height-ish, the epoch offset, the
        # count, then one 64-bit elapsed-realtime stamp per frame.
        return (capture.MAGIC_V2 + b"\0" * 4 + struct.pack("<q", epoch_offset_ns)
                + struct.pack("<I", len(frames_ns)) + stamps)
    return capture.MAGIC_V1 + struct.pack("<I", len(frames_ns)) + stamps


def sample_table(sizes, offsets):
    return box(
        b"stbl",
        box(b"stsz", FULL, struct.pack(">II", 0, len(sizes)),
            struct.pack(">%dI" % len(sizes), *sizes)),
        box(b"stco", FULL, struct.pack(">I", len(offsets)),
            struct.pack(">%dI" % len(offsets), *offsets)),
        # One chunk holding every sample, which is what a pulled recording has.
        box(b"stsc", FULL, struct.pack(">I", 1),
            struct.pack(">III", 1, len(sizes), 1)),
    )


def trak(handler, sizes, offsets):
    return box(
        b"trak",
        box(b"mdia",
            box(b"hdlr", FULL, b"\0\0\0\0", handler, b"\0" * 12, b"\0"),
            box(b"minf", sample_table(sizes, offsets))),
    )


def build_recording(path, frames_ns, version=2, epoch_offset_ns=0, metadata=True,
                    video=True):
    """Write an MP4 whose metadata track says `frames_ns` and nothing else.

    The chunk offset has to point past the `moov`, whose size depends on the
    offsets it holds, so the first pass measures it with a placeholder.
    """
    payload = winscope_payload(frames_ns, version, epoch_offset_ns)
    ftyp = box(b"ftyp", b"isom", b"\0\0\2\0", b"isomiso2avc1mp41")

    def moov(meta_offset):
        parts = []
        if metadata:
            parts.append(trak(b"meta", [len(payload)], [meta_offset]))
        if video:
            # Not decodable video - there is no picture in this file - but a
            # second track with another handler, as a real recording has.
            parts.append(trak(b"vide", [4], [meta_offset]))
        return box(b"moov", *parts)

    first = moov(0)
    start = len(ftyp) + len(first) + 8  # the mdat's own header
    final = moov(start)
    assert len(final) == len(first), "the placeholder offset changed the moov size"
    with open(path, "wb") as handle:
        handle.write(ftyp + final + box(b"mdat", payload))
    return path


# --- the tests ---------------------------------------------------------------

FRAMES = [51_000_000_000, 51_033_000_000, 51_100_000_000]  # 51.000s, 51.033s, 51.100s
PTS = [0.0, 0.033, 0.1]


class ParsingTest(unittest.TestCase):
    """What the metadata track is read as."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)

    def write(self, frames=FRAMES, **kwargs):
        return build_recording(os.path.join(self.directory.name, "recording.mp4"),
                               frames, **kwargs)

    def test_elapsed_clock_from_a_version_2_track(self):
        meta = capture.parse_metadata(self.write(epoch_offset_ns=1_700_000_000))
        self.assertEqual(meta["clock"], "elapsed")
        self.assertEqual(meta["unit"], 1e9)
        self.assertEqual(meta["frames"], FRAMES)
        self.assertEqual(meta["epoch_offset_ns"], 1_700_000_000)

    def test_monotonic_clock_from_a_version_1_track(self):
        # Version 1 stamps are the monotonic clock in nanoseconds too; the unit
        # it reports is what divides them back into seconds.
        meta = capture.parse_metadata(self.write(version=1))
        self.assertEqual(meta["clock"], "monotonic")
        self.assertEqual(meta["unit"], 1e6)
        self.assertEqual(meta["frames"], FRAMES)

    def test_elapsed_wins_over_monotonic_when_a_file_has_both(self):
        # A file with one of each: the elapsed clock is the one /proc/uptime
        # reports, so it is the one the mapping wants.
        path = os.path.join(self.directory.name, "both.mp4")
        ftyp = box(b"ftyp", b"isom", b"\0\0\2\0", b"isomiso2avc1mp41")
        old = winscope_payload(FRAMES, version=1)
        new = winscope_payload(FRAMES, version=2)
        moov_first = box(b"moov", trak(b"meta", [len(old)], [0]),
                         trak(b"meta", [len(new)], [0]))
        start = len(ftyp) + len(moov_first) + 8
        moov = box(b"moov", trak(b"meta", [len(old)], [start + len(old)]),
                   trak(b"meta", [len(new)], [start]))
        with open(path, "wb") as handle:
            handle.write(ftyp + moov + box(b"mdat", new + old))
        meta = capture.parse_metadata(path)
        self.assertEqual(meta["clock"], "elapsed")

    def test_no_metadata_track_is_none(self):
        self.assertIsNone(capture.parse_metadata(self.write(metadata=False)))

    def test_frames_out_of_order_stop_with_a_message(self):
        path = self.write([2_000_000_000, 1_000_000_000])
        with self.assertRaises(SystemExit):
            capture.parse_metadata(path)

    def test_a_truncated_file_does_not_raise(self):
        path = self.write()
        with open(path, "rb") as handle:
            data = handle.read()
        with open(path, "wb") as handle:
            handle.write(data[:len(data) // 2])
        # Half a recording is a parse that finds nothing, not a traceback.
        capture.parse_metadata(path)


class MappingTest(unittest.TestCase):
    """What an event becomes: a frame index and a time in the recording."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        build_recording(os.path.join(self.directory.name, capture.RECORDING), FRAMES)

    def map(self, events, pts=PTS, offset=50.0, frames=FRAMES):
        with open(os.path.join(self.directory.name, capture.EVENTS), "w") as handle:
            for event in events:
                handle.write(json.dumps(event) + "\n")
        sidecar = {"frames": list(frames),
                   "sync": {"clock": "elapsed", "unit_ns": 1_000_000_000,
                            "offset": offset}}
        with mock.patch.object(capture, "container_pts", return_value=list(pts)):
            return capture.map_events(self.directory.name, sidecar)

    def test_an_event_maps_to_the_last_frame_before_it(self):
        # Host 0.0 + offset 50.0 is device 50.0s; the frames start at 51.0s, so a
        # host time of 1.05 is 51.05s - after the second frame, before the third.
        events = [{"name": "one", "host": 1.0},
                  {"name": "two", "host": 1.033},
                  {"name": "three", "host": 1.05}]
        mapped = self.map(events)
        self.assertEqual([e["frame"] for e in mapped], [0, 1, 1])
        self.assertEqual([e["timed"] for e in mapped], ["host"] * 3)
        self.assertEqual([e["since_frame_ms"] for e in mapped], [0.0, 0.0, 17.0])

    def test_the_frame_carries_the_containers_own_time(self):
        events = [{"name": "two", "host": 1.033}, {"name": "three", "host": 1.05}]
        mapped = self.map(events)
        self.assertEqual([e["video_s"] for e in mapped], [0.033, 0.033])

    def test_an_event_the_device_timed_ignores_the_offset(self):
        # `device_ns` is already on the frames' clock: 1.09s is between the second
        # and third frame, and a wrong offset must not move it.
        events = [{"name": "pixel", "host": 0.0,
                   "data": {"device_ns": "51090000000"}}]
        mapped = self.map(events, offset=-1_000.0)
        self.assertEqual(mapped[0]["frame"], 1)
        self.assertEqual(mapped[0]["timed"], "device")
        self.assertEqual(mapped[0]["since_frame_ms"], 57.0)

    def test_an_event_before_the_first_frame_is_flagged(self):
        events = [{"name": "start", "host": 0.0}]  # device 50.0s, frames from 51.0
        mapped = self.map(events)
        self.assertEqual(mapped[0]["frame"], 0)
        self.assertTrue(mapped[0]["before_video"])
        self.assertLess(mapped[0]["since_frame_ms"], 0)

    def test_an_event_after_the_last_frame_stays_on_it(self):
        events = [{"name": "stop", "host": 1.5}]  # device 51.5s, last frame 51.1
        mapped = self.map(events)
        self.assertEqual(mapped[0]["frame"], 2)
        # The picture is the last frame's, and so is the time: it was still what
        # was on screen half a second later.
        self.assertEqual(mapped[0]["video_s"], 0.1)
        self.assertEqual(mapped[0]["since_frame_ms"], 400.0)

    def test_a_container_with_fewer_frames_extends_its_last_time(self):
        # ffprobe read two frames where the metadata track has three (it drops
        # one the decoder could not hand over); the third frame's time is the
        # last known one plus the gap the metadata reports.
        events = [{"name": "stop", "host": 1.5}]
        mapped = self.map(events, pts=[0.0, 0.033])
        self.assertEqual(mapped[0]["frame"], 2)
        self.assertEqual(mapped[0]["video_s"], 0.433)

    def test_a_repeated_name_keeps_every_moment(self):
        events = [{"name": "check", "host": 1.0}, {"name": "check", "host": 1.05}]
        mapped = self.map(events)
        self.assertEqual([e["frame"] for e in mapped], [0, 1])

    def test_data_is_carried_through(self):
        events = [{"name": "typed", "host": 1.0,
                   "data": {"device_ns": "51010000000", "keys": "2"}}]
        mapped = self.map(events)
        self.assertEqual(mapped[0]["data"], {"device_ns": "51010000000", "keys": "2"})

    def test_without_container_times_the_frame_index_still_maps(self):
        # No ffprobe (or a file it cannot read): there is no presentation time to
        # report, but the index into the metadata track is still known, and the
        # mapping must not fail on the missing pts.
        events = [{"name": "one", "host": 1.0}]
        mapped = self.map(events, pts=[])
        self.assertEqual(mapped[0]["frame"], 0)
        self.assertNotIn("video_s", mapped[0])

    def test_without_a_metadata_track_nothing_is_mapped(self):
        events = [{"name": "one", "host": 1.0}]
        with open(os.path.join(self.directory.name, capture.EVENTS), "w") as handle:
            handle.write(json.dumps(events[0]) + "\n")
        sidecar = {"frames": [], "sync": {"clock": "none", "accuracy_ms": None}}
        mapped = capture.map_events(self.directory.name, sidecar)
        self.assertEqual(mapped, [{"name": "one", "host": 1.0}])

    def test_a_broken_line_in_the_events_is_skipped(self):
        with open(os.path.join(self.directory.name, capture.EVENTS), "w") as handle:
            handle.write('{"name": "one", "host": 1.0}\n')
            handle.write("not json\n")
            handle.write('{"name": "two", "host": 1.05}\n')
        sidecar = {"frames": FRAMES, "sync": {"clock": "elapsed",
                                             "unit_ns": 1_000_000_000,
                                             "offset": 50.0}}
        with mock.patch.object(capture, "container_pts", return_value=PTS):
            mapped = capture.map_events(self.directory.name, sidecar)
        self.assertEqual([e["name"] for e in mapped], ["one", "two"])


class ContainerPtsTest(unittest.TestCase):
    def test_no_ffprobe_is_no_container_times(self):
        # Not every machine that runs this has ffmpeg, and a traceback is not an
        # answer: without it the mapping reports frame indices and nothing more.
        with mock.patch.object(capture.subprocess, "run",
                               side_effect=FileNotFoundError("ffprobe")):
            self.assertEqual(capture.container_pts("whatever.mp4"), [])

    def test_ffprobe_refusing_the_file_is_no_container_times(self):
        done = mock.Mock(returncode=1, stdout="", stderr="Invalid data")
        with mock.patch.object(capture.subprocess, "run", return_value=done):
            self.assertEqual(capture.container_pts("whatever.mp4"), [])


class FrameCommandTest(unittest.TestCase):
    def test_it_seeks_just_before_the_frame(self):
        command = capture.frame_command("recording.mp4", 12.5, "out.png")
        self.assertEqual(command[command.index("-ss") + 1], "12.499000")
        # One frame out of the video stream, into the named file.
        self.assertEqual(command[command.index("-frames:v") + 1], "1")
        self.assertEqual(command[-1], "out.png")

    def test_a_time_at_zero_does_not_seek_negative(self):
        command = capture.frame_command("recording.mp4", 0.0, "out.png")
        self.assertEqual(command[command.index("-ss") + 1], "0.000000")


if __name__ == "__main__":
    unittest.main()
