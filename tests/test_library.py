"""Tests for the library surface: connect.find/connect, capture.Session and
watch.Watcher.

The phone is faked here as it is in test_cli: adb is a Mock, mDNS is a Mock and
the recording is a file wrote by a fake strategy. What is checked is what a test
suite embedding this package sees - return values, exceptions, an event that
costs no process, and a watcher whose state can be asked about in process.
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

from adb_harness import capture, connect, watch  # noqa: E402
from adb_harness.errors import (CaptureFailed, NoAdb, NoMdns,  # noqa: E402
                                NotConnected)


def run_main(module, argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = module.main(argv)
        except SystemExit as stopped:
            code = stopped.code if isinstance(stopped.code, int) else 1
    return code, out.getvalue(), err.getvalue()


class ConnectLibraryTest(unittest.TestCase):
    """`find` returns a Port and `connect` a ConnectResult, without printing."""

    def info(self, port=38109, name="adb-phone._adb-tls-connect._tcp.local."):
        info = mock.Mock()
        info.port = port
        info.name = name
        return info

    def environment(self, devices=(), found=(), side_effect=None, adb=None):
        lookup = (mock.Mock(side_effect=side_effect) if side_effect is not None
                  else mock.Mock(return_value=list(devices)))
        return mock.patch.multiple(
            connect,
            shutil=mock.Mock(which=mock.Mock(return_value="/usr/bin/adb")),
            discover=mock.Mock(return_value=(list(found), [])),
            addresses=mock.Mock(return_value=["127.0.0.1"]),
            attached=lookup,
            adb=adb or mock.Mock(return_value="connected to it"),
        )

    def test_find_returns_the_advertised_port_and_connect_it(self):
        info = self.info()
        adb = mock.Mock(return_value="connected to it")
        with self.environment(found=[info],
                              side_effect=[[], ["127.0.0.1:38109"]],
                              adb=adb):
            port = connect.find(timeout=0.1)
            result = connect.connect(port)
        self.assertIsInstance(port, connect.Port)
        self.assertEqual(port.address, "127.0.0.1:38109")
        self.assertEqual(port.port, 38109)
        self.assertEqual(port.source, "mdns")
        self.assertEqual(port.name, info.name)
        self.assertEqual(result.serial, "127.0.0.1:38109")
        self.assertEqual(result.address, "127.0.0.1:38109")
        self.assertFalse(result.already)
        adb.assert_called_once_with("connect", "127.0.0.1:38109")

    def test_find_returns_none_when_nothing_is_advertised(self):
        with self.environment(devices=[]):
            self.assertIsNone(connect.find(timeout=0.1))

    def test_an_attached_device_is_a_port_without_touching_adb(self):
        adb = mock.Mock(return_value="should not run")
        with self.environment(devices=["first", "second"], adb=adb):
            port = connect.find(serial="second")
            result = connect.connect(port)
        self.assertEqual(port.source, "attached")
        self.assertEqual(port.serial, "second")
        self.assertEqual(result.serial, "second")
        self.assertTrue(result.already)
        adb.assert_not_called()

    def test_a_named_port_is_a_port_with_source_port(self):
        with self.environment(devices=["127.0.0.1:9999"]):
            port = connect.find(port=9999)
        self.assertEqual(port.source, "port")
        self.assertEqual(port.address, "127.0.0.1:9999")
        self.assertEqual(port.port, 9999)

    def test_no_adb_raises_no_adb(self):
        with mock.patch.object(connect.shutil, "which", return_value=None):
            with self.assertRaises(NoAdb):
                connect.find()

    def test_no_zeroconf_raises_no_mdns(self):
        with mock.patch.object(connect.shutil, "which",
                               return_value="/usr/bin/adb"), \
                mock.patch.object(connect, "attached", return_value=[]), \
                mock.patch.dict(sys.modules, {"zeroconf": None}):
            with self.assertRaises(NoMdns):
                connect.find(timeout=0.1)

    def test_a_port_nothing_answers_raises_not_connected(self):
        info = self.info()
        with self.environment(found=[info], devices=[],
                              adb=mock.Mock(return_value="failed to connect: Connection refused")):
            port = connect.find(timeout=0.1)
            with self.assertRaises(NotConnected) as caught:
                connect.connect(port)
        self.assertEqual(caught.exception.port, 38109)
        self.assertIn("failed to connect", caught.exception.said)


class CaptureLibraryTest(unittest.TestCase):
    """`Session` starts, appends events and stops without the CLI in the way."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dir = self.directory.name
        self.video = os.path.join(self.dir, capture.RECORDING)

    def fake_stop(self):
        def stop(args, state, serial):
            with open(self.video, "wb") as handle:
                handle.write(b"mp4\n")
            meta = {"clock": "elapsed", "unit": 1e9,
                    "frames": [51_000_000_000, 51_033_000_000]}
            return self.video, meta, None
        return stop

    def session(self, **kwargs):
        return capture.Session(dir=self.dir, serial="S", source="stream", **kwargs)

    def test_a_hundred_events_cost_no_process_and_less_than_fifty_ms(self):
        adb = mock.Mock(return_value=mock.Mock(returncode=0))
        with mock.patch.object(capture.shutil, "which",
                               return_value="/usr/bin/adb"), \
                mock.patch.object(capture, "adb", adb), \
                mock.patch.object(capture, "sample_clock",
                                  return_value={"offset": 50.0, "error": 0.0}), \
                mock.patch.object(capture, "host_clock", return_value=1.0), \
                mock.patch.object(capture, "container_pts",
                                  return_value=[0.0, 0.033]), \
                mock.patch.object(capture.Stream, "start",
                                  return_value={"reader_pid": 0}), \
                mock.patch.object(capture.Stream, "stop",
                                  side_effect=self.fake_stop()) as stop:
            session = self.session()
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                session.start()
                calls_after_start = adb.call_count
                started = time.perf_counter()
                for index in range(100):
                    session.event("check:diag", {"index": str(index)})
                elapsed = time.perf_counter() - started
                self.assertEqual(adb.call_count, calls_after_start)
                recording = session.stop()
        self.assertLess(elapsed, 0.05)
        stop.assert_called_once()
        self.assertEqual(len(recording.events), 101)  # record:start plus 100
        self.assertTrue(os.path.isfile(recording.sidecar))
        self.assertEqual(recording.data["events"][1]["data"], {"index": "0"})
        frame = recording.frame_for("check:diag")
        self.assertIsNotNone(frame)
        self.assertEqual(frame["frame"], 0)
        self.assertEqual(frame["timed"], "host")
        self.assertEqual(len(recording.frames_for("check:diag")), 100)
        self.assertIsNone(recording.frame_for("check:absent"))

    def test_stop_writes_the_sidecar_and_is_idempotent(self):
        with mock.patch.object(capture.shutil, "which",
                               return_value="/usr/bin/adb"), \
                mock.patch.object(capture, "adb",
                                  return_value=mock.Mock(returncode=0)), \
                mock.patch.object(capture, "sample_clock",
                                  return_value={"offset": 50.0, "error": 0.0}), \
                mock.patch.object(capture, "host_clock", return_value=1.0), \
                mock.patch.object(capture, "container_pts",
                                  return_value=[0.0, 0.033]), \
                mock.patch.object(capture.Stream, "start",
                                  return_value={"reader_pid": 0}), \
                mock.patch.object(capture.Stream, "stop",
                                  side_effect=self.fake_stop()):
            session = self.session()
            session.start()
            session.event("one")
            first = session.stop()
            second = session.stop()
        self.assertIs(first, second)
        with open(first.sidecar) as handle:
            sidecar = json.load(handle)
        self.assertEqual(sidecar["source"], "stream")
        self.assertEqual(sidecar["events"][1]["name"], "one")

    def test_an_event_before_start_is_refused(self):
        session = capture.Session(dir=self.dir, serial="S")
        with self.assertRaises(CaptureFailed):
            session.event("too-early")

    def test_the_context_manager_stops_the_recording(self):
        with mock.patch.object(capture.shutil, "which",
                               return_value="/usr/bin/adb"), \
                mock.patch.object(capture, "adb",
                                  return_value=mock.Mock(returncode=0)), \
                mock.patch.object(capture, "sample_clock",
                                  return_value={"offset": 50.0, "error": 0.0}), \
                mock.patch.object(capture, "host_clock", return_value=1.0), \
                mock.patch.object(capture, "container_pts",
                                  return_value=[0.0, 0.033]), \
                mock.patch.object(capture.Stream, "start",
                                  return_value={"reader_pid": 0}), \
                mock.patch.object(capture.Stream, "stop",
                                  side_effect=self.fake_stop()):
            with self.session() as session:
                session.event("inside")
        self.assertTrue(os.path.isfile(os.path.join(self.dir, "recording.json")))


class CaptureSerialTest(unittest.TestCase):
    """Every capture subcommand takes --serial, and stop uses it."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dir = self.directory.name

    def test_every_subcommand_accepts_serial(self):
        parser = capture.build_parser()
        for argv in (["start", "--serial", "B"],
                     ["event", "--serial", "B", "a-name"],
                     ["stop", "--serial", "B"],
                     ["show", "--serial", "B"],
                     ["frames", "--serial", "B"],
                     ["read", "--serial", "B"]):
            args = parser.parse_args(argv)
            self.assertEqual(args.sub_serial, "B", argv)

    def test_start_records_the_named_serial_of_two_devices(self):
        clock = mock.Mock(return_value={"offset": 0.0, "error": 0.0})
        with mock.patch.object(capture.shutil, "which",
                               return_value="/usr/bin/adb"), \
                mock.patch.object(capture, "adb",
                                  return_value=mock.Mock(returncode=0)) as adb, \
                mock.patch.object(capture, "sample_clock", clock), \
                mock.patch.object(capture, "host_clock", return_value=1.0), \
                mock.patch.object(capture.Stream, "start",
                                  return_value={"reader_pid": 0}):
            code, out, err = run_main(
                capture, ["start", "--dir", self.dir, "--serial", "B",
                          "--source", "stream", "--json"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(json.loads(out)["serial"], "B")
        clock.assert_called_with("B")
        adb.assert_any_call(["get-state"], "B")
        with open(os.path.join(self.dir, capture.STATE)) as handle:
            self.assertEqual(json.load(handle)["serial"], "B")

    def test_stop_uses_the_serial_it_was_given(self):
        video = os.path.join(self.dir, capture.RECORDING)
        with open(video, "wb") as handle:
            handle.write(b"mp4\n")
        state = {"source": "screenrecord", "serial": "A", "started": 0,
                 "host_clock": 0.0, "clock": [{"offset": 0.0, "error": 0.0}],
                 "remote": capture.REMOTE}
        with open(os.path.join(self.dir, capture.STATE), "w") as handle:
            json.dump(state, handle)
        with open(os.path.join(self.dir, capture.EVENTS), "w") as handle:
            handle.write(json.dumps({"name": "one", "host": 1.0}) + "\n")
        stop = mock.Mock(return_value=(video, None, None))
        with mock.patch.object(capture.shutil, "which",
                               return_value="/usr/bin/adb"), \
                mock.patch.object(capture, "sample_clock",
                                  return_value={"offset": 0.0, "error": 0.0}) as clock, \
                mock.patch.object(capture, "container_pts", return_value=[]), \
                mock.patch.object(capture.Screenrecord, "stop", stop):
            code, out, err = run_main(
                capture, ["stop", "--dir", self.dir, "--serial", "B"])
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(stop.call_args[0][2], "B")
        clock.assert_called_with("B")


class WatcherLibraryTest(unittest.TestCase):
    """`Watcher` runs in process and answers status() while it does."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.out = self.directory.name

    def test_status_reports_running_and_attempts_in_process(self):
        seen = []
        watcher = None

        def on_event(event):
            seen.append((event, watcher.status()))

        watcher = watch.Watcher(interval=0.01, max_attempts=1, on_event=on_event,
                                out=self.out, serial="S")
        calls = []

        def fake_devices():
            calls.append(True)
            return [] if len(calls) == 1 else ["S"]

        watcher._devices = fake_devices
        watcher._adb_start = mock.Mock()
        watcher._reconnect = mock.Mock(return_value=("connected to S", 0))
        code = watcher.run()

        self.assertEqual(code, 0)
        self.assertEqual(len(seen), 1)
        event, during = seen[0]
        self.assertEqual(event.event, "reconnected")
        self.assertEqual(event.attempts, 1)
        self.assertTrue(during.running)
        self.assertEqual(during.pid, os.getpid())
        self.assertEqual(during.devices, ("S",))
        self.assertEqual(during.attempts, 1)
        after = watcher.status()
        self.assertFalse(after.running)
        self.assertIsNone(after.pid)
        self.assertEqual(after.attempts, 1)
        with open(os.path.join(self.out, "adb-watch.attempts")) as handle:
            self.assertEqual(handle.read().strip(), "1")
        self.assertFalse(os.path.exists(os.path.join(self.out, "adb-watch.pid")))

    def test_stop_before_run_returns_immediately(self):
        watcher = watch.Watcher(interval=1, out=self.out)
        watcher.stop()
        self.assertEqual(watcher.run(), 0)

    def test_a_bad_interval_is_refused(self):
        for value in (0, -1, "soon"):
            with self.assertRaises(ValueError):
                watch.Watcher(interval=value)


if __name__ == "__main__":
    unittest.main()
