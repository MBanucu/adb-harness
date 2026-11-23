"""Tests for the watcher loop and `--status` without a device.

The reconnect command and `adb devices` are executables written to a
TemporaryDirectory by `tests/fakeadb.py`, so `Watcher.run`, the attempt file, the
log and the deduplication all run as they do after a reboot. The interval is
milliseconds, not the default fifteen seconds: three ticks finish in well under
two seconds.
"""

import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adb_harness import watch  # noqa: E402
from fakeadb import FakeAdb  # noqa: E402

FAILED = {"output": "nothing advertised _adb-tls-connect._tcp.local.", "code": 1}
ATTACHED = "127.0.0.1:38109"


def run_main(module, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = module.main(argv)
        except SystemExit as stopped:
            code = stopped.code if isinstance(stopped.code, int) else 1
    return code, out.getvalue(), err.getvalue()


class WatcherTickTest(unittest.TestCase):
    """Three ticks against the fake adb, with no `sleep 15`."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.out = self.directory.name
        self.fake = FakeAdb(self.out, {"devices": [], "adb_connect": FAILED})
        self.env = mock.patch.dict(
            os.environ,
            {**self.fake.environment(), "ADB_WATCH_OUT": self.out})
        self.env.start()
        self.addCleanup(self.env.stop)

    def watcher(self, **kwargs):
        events = []
        watcher = watch.Watcher(interval=0.001, out=self.out,
                                on_event=events.append, **kwargs)
        return watcher, events

    def test_no_device_connects_once_per_tick_and_does_not_sleep(self):
        watcher, events = self.watcher(max_attempts=3)
        started = time.monotonic()
        code = watcher.run()
        elapsed = time.monotonic() - started

        self.assertEqual(code, 1)
        self.assertLess(elapsed, 2.0)
        self.assertEqual(len(self.fake.calls("adb-connect")), 3)
        self.assertEqual(watcher.status().attempts, 3)
        with open(os.path.join(self.out, "adb-watch.attempts")) as handle:
            self.assertEqual(handle.read().strip(), "3")
        # The same failure three times is one log line, not three.
        self.assertEqual([event.event for event in events], ["error"])
        self.assertEqual(events[0].attempts, 1)
        self.assertIn("nothing advertised", events[0].detail)
        self.assertFalse(os.path.exists(os.path.join(self.out, "adb-watch.pid")))

    def test_a_repeated_failure_is_logged_once_within_ten_minutes(self):
        watcher, events = self.watcher(max_attempts=3)
        # Three ticks: the first and the one ten minutes later are logged.
        with mock.patch.object(watch.time, "time", side_effect=[0.0, 1.0, 601.0]):
            self.assertEqual(watcher.run(), 1)
        self.assertEqual([event.event for event in events], ["error", "error"])
        self.assertEqual([event.attempts for event in events], [1, 3])
        self.assertEqual(watcher.attempts, 3)

    def test_a_device_that_appears_exits_and_reports_attached(self):
        self.fake.reset({
            "devices": [],
            "adb_connect": {
                "output": "connected to %s" % ATTACHED,
                "code": 0,
                "add": [ATTACHED],
            },
        })
        watcher, events = self.watcher(max_attempts=1)
        self.assertEqual(watcher.run(), 0)
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event.event, "reconnected")
        self.assertEqual(event.serial, ATTACHED)
        self.assertEqual(event.devices, (ATTACHED,))
        self.assertEqual(event.attempts, 1)
        self.assertEqual(self.fake.devices(), [(ATTACHED, "device")])

    def test_the_error_stream_is_one_object_per_state_change(self):
        code, out, err = run_main(
            watch, ["--json", "--quiet", "--interval", "0.001",
                    "--max-attempts", "3"])
        lines = [json.loads(line) for line in out.splitlines() if line.strip()]
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["event"], "error")
        self.assertEqual(lines[0]["attempts"], 1)
        self.assertIn("nothing advertised", lines[0]["detail"])

    def test_the_log_does_not_repeat_the_same_failure(self):
        log = os.path.join(self.out, "adb-watch.log")
        with mock.patch.dict(os.environ, {"ADB_WATCH_LOG": log}):
            code, _out, _err = run_main(
                watch, ["--json", "--interval", "0.001",
                        "--max-attempts", "3"])
        self.assertEqual(code, 1)
        with open(log) as handle:
            lines = [line for line in handle if "not connected:" in line]
        self.assertEqual(len(lines), 1)


class StatusTest(unittest.TestCase):
    """`--status` reads the pidfile, and says so when there is none."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.out = self.directory.name
        self.fake = FakeAdb(self.out, {
            "devices": [[ATTACHED, "device"]]})
        self.env = mock.patch.dict(
            os.environ,
            {**self.fake.environment(), "ADB_WATCH_OUT": self.out})
        self.env.start()
        self.addCleanup(self.env.stop)

    def status(self, *argv):
        return run_main(watch, ["--status", *argv])

    def test_status_with_a_pidfile_is_running(self):
        with open(os.path.join(self.out, "adb-watch.pid"), "w") as handle:
            handle.write("%d\n" % os.getpid())
        with open(os.path.join(self.out, "adb-watch.attempts"), "w") as handle:
            handle.write("4\n")
        code, out, err = self.status("--json")
        data = json.loads(out)
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(data["running"])
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(data["devices"], [ATTACHED])
        self.assertEqual(data["attempts"], 4)

    def test_status_without_a_pidfile_is_not_running(self):
        code, out, err = self.status("--json")
        data = json.loads(out)
        self.assertEqual((code, err), (1, ""))
        self.assertFalse(data["running"])
        self.assertIsNone(data["pid"])
        self.assertEqual(data["devices"], [ATTACHED])
        self.assertEqual(data["attempts"], 0)

    def test_a_stale_pidfile_is_not_running(self):
        # A pid no process can have: the watcher died without removing it.
        with open(os.path.join(self.out, "adb-watch.pid"), "w") as handle:
            handle.write("999999999\n")
        code, out, _err = self.status("--json")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(out)["running"])

    def test_human_status_without_a_pidfile_is_unchanged(self):
        code, out, err = self.status()
        self.assertEqual((code, err), (1, ""))
        self.assertEqual(out, "adb-watch not running\n")


if __name__ == "__main__":
    unittest.main()
