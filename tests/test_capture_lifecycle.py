"""Tests for the capture lifecycle's failures, without a device.

A `screenrecord` start runs against `tests/fakeadb.py`'s `adb`, which answers
`get-state` and the device shell (including `/proc/uptime`), so no screen, no
ffmpeg and no recorder are involved. The rest of the module checks the errors a
harness embedding `capture.Session` sees and the exit codes the commands return
for them.
"""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adb_harness import capture  # noqa: E402
from adb_harness.errors import CaptureFailed  # noqa: E402
from fakeadb import FakeAdb  # noqa: E402


def run_main(module, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = module.main(argv)
        except SystemExit as stopped:
            code = stopped.code if isinstance(stopped.code, int) else 1
    return code, out.getvalue(), err.getvalue()


class CaptureLifecycleTest(unittest.TestCase):
    """The order of start, event and stop, and what a wrong order says."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dir = os.path.join(self.directory.name, "capture")
        self.fake = FakeAdb(self.directory.name, {
            "devices": [["127.0.0.1:38109", "device"]],
            "get_state": {"output": "device", "code": 0},
            "shell": {"output": "1000.0 2000.0\n", "code": 0},
        })
        self.env = mock.patch.dict(os.environ, self.fake.environment())
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_stop_without_start_is_capture_failed(self):
        session = capture.Session(dir=self.dir)
        with self.assertRaisesRegex(CaptureFailed, "run start first"):
            session.stop()
        self.assertFalse(os.path.exists(os.path.join(self.dir, capture.STATE)))

    def test_stop_without_start_is_exit_one_from_the_command(self):
        code, out, err = run_main(capture, ["stop", "--dir", self.dir])
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertEqual(err,
                         "capture: no recording in %s (run start first)\n"
                         % self.dir)

    def test_two_starts_on_one_session_are_refused(self):
        session = capture.Session(dir=self.dir, source="screenrecord")
        session.start()
        with open(os.path.join(self.dir, capture.STATE)) as handle:
            first_state = json.load(handle)
        with self.assertRaisesRegex(CaptureFailed, "already recording"):
            session.start()
        # The refused second start did not overwrite the recording in progress.
        with open(os.path.join(self.dir, capture.STATE)) as handle:
            self.assertEqual(json.load(handle), first_state)

    def test_an_event_before_start_is_refused_in_the_library(self):
        session = capture.Session(dir=self.dir)
        with self.assertRaisesRegex(CaptureFailed, "run start first"):
            session.event("too-early")
        self.assertFalse(os.path.exists(os.path.join(self.dir, capture.EVENTS)))

    def test_an_event_before_start_is_a_no_op_from_the_command(self):
        # A harness calls `event` unconditionally; before `start` there is no
        # recording to mark, and making the command fail would break the run.
        code, out, err = run_main(
            capture, ["event", "--dir", self.dir, "too-early"])
        self.assertEqual((code, out, err), (0, "", ""))
        self.assertFalse(os.path.exists(os.path.join(self.dir, capture.EVENTS)))

    def test_show_without_a_recording_is_no_recording(self):
        code, out, err = run_main(capture, ["show", "--dir", self.dir, "--json"])
        data = json.loads(out)
        self.assertEqual((code, err), (1, ""))
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "no-recording")
        self.assertIn("run stop first", data["detail"])

    def test_show_without_a_recording_says_so_in_prose(self):
        code, out, err = run_main(capture, ["show", "--dir", self.dir])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no recording.json", err)

    def test_a_sidecar_whose_video_is_missing_is_exit_one(self):
        os.makedirs(self.dir)
        sidecar = {"recording": "recording.mp4", "source": "screenrecord",
                   "size": 4, "duration": 0.0, "frames": [], "events": [],
                   "sync": {"clock": "none", "accuracy_ms": None}}
        with open(os.path.join(self.dir, capture.SIDECAR), "w") as handle:
            json.dump(sidecar, handle)
        # ffmpeg only has to be on PATH to reach the missing-file check; it is
        # never run for a recording that is not there.
        with mock.patch.object(capture.shutil, "which",
                               return_value="/usr/bin/ffmpeg"):
            code, out, err = run_main(
                capture, ["frames", "--dir", self.dir, "--all"])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no recording at", err)
        self.assertIn("recording.mp4", err)

    def test_frames_without_a_sidecar_is_exit_one(self):
        with mock.patch.object(capture.shutil, "which",
                               return_value="/usr/bin/ffmpeg"):
            code, out, err = run_main(capture, ["frames", "--dir", self.dir])
        self.assertEqual((code, out), (1, ""))
        self.assertIn("no recording.json; run stop first", err)


if __name__ == "__main__":
    unittest.main()
