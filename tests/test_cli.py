"""Tests for the machine-readable output, the exit codes and the human output.

The phone is faked throughout: `adb`, mDNS and the stream's start are replaced,
so what is checked is the contract a harness sees - one JSON object, the same
exit code for the same kind of failure, and prose that has not moved.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from adb_harness import capture, connect, watch  # noqa: E402


def run_main(module, argv):
    """The command's return code, with both streams kept for inspection."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = module.main(argv)
        except SystemExit as stopped:  # argparse --version and --help
            code = stopped.code if isinstance(stopped.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def attached_after(serial):
    """`adb devices` empty before the connect, one device after it."""
    seen = []

    def lookup():
        seen.append(True)
        return [] if len(seen) == 1 else [serial]

    return lookup


class ConnectJsonTest(unittest.TestCase):
    """`adb-connect --json`, when the phone is there and when it is not."""

    def info(self, port=38109, name="adb-phone._adb-tls-connect._tcp.local."):
        info = mock.Mock()
        info.port = port
        info.name = name
        return info

    def environment(self, devices=(), found=(), pairing=(), said="connected to it",
                    attached=None):
        """adb, mDNS and zeroconf as fakes, without a phone or a network."""
        lookup = (mock.Mock(side_effect=attached) if attached is not None
                  else mock.Mock(return_value=list(devices)))
        return mock.patch.multiple(
            connect,
            shutil=mock.Mock(which=mock.Mock(return_value="/usr/bin/adb")),
            discover=mock.Mock(return_value=(list(found), list(pairing))),
            addresses=mock.Mock(return_value=["127.0.0.1"]),
            attached=lookup,
            adb=mock.Mock(return_value=said),
        )

    def run_connect(self, argv, **kwargs):
        zeroconf = types.ModuleType("zeroconf")
        with self.environment(**kwargs), \
                mock.patch.dict(sys.modules, {"zeroconf": zeroconf}):
            return run_main(connect, argv)

    def test_connected_json_is_one_object_with_the_port(self):
        info = self.info()
        code, out, err = self.run_connect(
            ["--json"], found=[info], attached=attached_after("127.0.0.1:38109"))
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(data["schema"], "adb-connect")
        self.assertEqual(data["version"], 1)
        self.assertTrue(data["ok"])
        self.assertEqual(data["address"], "127.0.0.1:38109")
        self.assertEqual(data["port"], 38109)
        self.assertEqual(data["serial"], "127.0.0.1:38109")
        self.assertEqual(data["source"], "mdns")
        self.assertIsInstance(data["waited_s"], float)

    def test_the_acceptance_pipeline_reads_the_port(self):
        # The command's stdout through the exact one-liner from the issue.
        info = self.info()
        code, out, _ = self.run_connect(
            ["--json"], found=[info], attached=attached_after("127.0.0.1:38109"))
        self.assertEqual(code, 0)
        shown = subprocess.run(
            [sys.executable, "-c",
             'import json,sys; print(json.load(sys.stdin)["port"])'],
            input=out, capture_output=True, text=True)
        self.assertEqual(shown.returncode, 0)
        self.assertEqual(shown.stdout.strip(), "38109")

    def test_unplugged_json_says_why_and_has_no_port(self):
        code, out, err = self.run_connect(["--json"])
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "no-service")
        self.assertIsNone(data["port"])
        self.assertIn("nothing advertised", data["detail"])

    def test_a_stale_port_is_a_connect_failure(self):
        info = self.info()
        code, out, _ = self.run_connect(
            ["--json"], found=[info], said="failed to connect: Connection refused")
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "stale-port")
        self.assertEqual(data["port"], 38109)
        self.assertIn("Connection refused", data["detail"])

    def test_no_adb_is_a_missing_prerequisite(self):
        with mock.patch.object(connect.shutil, "which", return_value=None), \
                mock.patch.dict(sys.modules, {"zeroconf": types.ModuleType("zeroconf")}):
            code, out, err = run_main(connect, ["--json"])
        data = json.loads(out)
        self.assertEqual(code, 3)
        self.assertEqual(err, "")
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "no-adb")

    def test_no_zeroconf_is_a_missing_prerequisite(self):
        with mock.patch.object(connect.shutil, "which", return_value="/usr/bin/adb"), \
                mock.patch.dict(sys.modules, {"zeroconf": None}):
            code, out, err = run_main(connect, ["--json"])
        data = json.loads(out)
        self.assertEqual(code, 3)
        self.assertEqual(err, "")
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "no-mdns")

    def test_an_already_attached_device_keeps_the_port_key(self):
        code, out, _ = self.run_connect(
            ["--json"], devices=["127.0.0.1:38109"])
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(data["ok"])
        self.assertEqual(data["source"], "attached")
        self.assertEqual(data["port"], 38109)

    def test_a_named_port_reports_its_source(self):
        code, out, _ = self.run_connect(
            ["--json", "--port", "9999"], devices=["127.0.0.1:9999"])
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(data["source"], "port")
        self.assertEqual(data["port"], 9999)

    def test_a_named_port_that_nothing_answers_is_a_failure(self):
        code, out, _ = self.run_connect(
            ["--json", "--port", "9999"], said="failed to connect")
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "stale-port")
        self.assertEqual(data["port"], 9999)

    def test_quiet_keeps_the_json_object_and_drops_progress(self):
        info = self.info()
        code, out, _ = self.run_connect(
            ["--json", "--quiet"], found=[info],
            attached=attached_after("127.0.0.1:38109"))
        self.assertEqual(code, 0)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertNotIn("asking mDNS", out)

    def test_version_is_printed(self):
        code, out, _ = run_main(connect, ["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "adb-connect %s" % connect.__version__)


class HumanConnectTest(unittest.TestCase):
    """The prose of the no-flag command, byte for byte."""

    def setUp(self):
        self.zeroconf = mock.patch.dict(
            sys.modules, {"zeroconf": types.ModuleType("zeroconf")})
        self.zeroconf.start()
        self.addCleanup(self.zeroconf.stop)

    def info(self):
        info = mock.Mock()
        info.port = 38109
        info.name = "adb-phone._adb-tls-connect._tcp.local."
        return info

    def test_success_is_the_same_three_lines(self):
        with mock.patch.object(connect.shutil, "which", return_value="/usr/bin/adb"), \
                mock.patch.object(connect, "discover",
                                  return_value=([self.info()], [])), \
                mock.patch.object(connect, "addresses",
                                  return_value=["127.0.0.1"]), \
                mock.patch.object(connect, "attached",
                                  side_effect=attached_after("127.0.0.1:38109")), \
                mock.patch.object(connect, "adb", return_value="connected"):
            code, out, err = run_main(connect, [])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(out,
                         "asking mDNS for the wireless-debugging port (8s)…\n"
                         "connected to 127.0.0.1:38109\n"
                         "  advertised as adb-phone._adb-tls-connect._tcp.local.\n"
                         "  device: 127.0.0.1:38109\n")

    def test_nothing_advertised_is_the_same_two_lines(self):
        with mock.patch.object(connect.shutil, "which", return_value="/usr/bin/adb"), \
                mock.patch.object(connect, "discover", return_value=([], [])), \
                mock.patch.object(connect, "attached", return_value=[]):
            code, out, err = run_main(connect, [])
        self.assertEqual(code, 1)
        self.assertEqual(out,
                         "asking mDNS for the wireless-debugging port (8s)…\n")
        self.assertEqual(err,
                         "nothing advertised _adb-tls-connect._tcp.local. or "
                         "_adb._tcp.local.\n"
                         "Wireless debugging is off, or its port was not picked "
                         "up yet.\n")

    def test_quiet_keeps_the_final_lines(self):
        with mock.patch.object(connect.shutil, "which", return_value="/usr/bin/adb"), \
                mock.patch.object(connect, "discover",
                                  return_value=([self.info()], [])), \
                mock.patch.object(connect, "addresses",
                                  return_value=["127.0.0.1"]), \
                mock.patch.object(connect, "attached",
                                  side_effect=attached_after("127.0.0.1:38109")), \
                mock.patch.object(connect, "adb", return_value="connected"):
            code, out, _ = run_main(connect, ["--quiet"])
        self.assertEqual(code, 0)
        self.assertNotIn("asking mDNS", out)
        self.assertTrue(out.startswith("connected to 127.0.0.1:38109\n"))


class CaptureJsonTest(unittest.TestCase):
    """`capture start --json`, `capture show --json`, and the exit table."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.dir = self.directory.name

    def start(self, *argv, stream=None):
        stream = stream or mock.patch.object(
            capture.Stream, "start", return_value={"reader_pid": 1234})
        with mock.patch.object(capture.shutil, "which",
                               return_value="/usr/bin/adb"), \
                mock.patch.object(capture, "adb",
                                  return_value=mock.Mock(returncode=0)), \
                mock.patch.object(capture, "sample_clock",
                                  return_value={"offset": 0.0, "error": 0.0}), \
                mock.patch.object(capture, "host_clock", return_value=1.0), \
                mock.patch.object(capture.Screenrecord, "start",
                                  return_value={"remote": capture.REMOTE}), \
                stream:
            return run_main(capture, ["start", "--dir", self.dir, *argv])

    def test_start_json_names_the_recording_and_its_source(self):
        code, out, err = self.start("--json")
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(data["schema"], "capture-start")
        self.assertEqual(data["version"], 1)
        self.assertTrue(data["recording"])
        self.assertEqual(data["source"], "stream")
        self.assertEqual(data["dir"], self.dir)
        self.assertTrue(os.path.isfile(os.path.join(self.dir, capture.STATE)))

    def test_quiet_hides_the_fallback_but_keeps_the_final_line(self):
        fallback = mock.patch.object(
            capture.Stream, "start",
            side_effect=capture.SourceUnavailable("no app"))
        code, out, err = self.start("--quiet", stream=fallback)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertNotIn("falling back", out)
        self.assertIn("screenrecord recording", out)

    def test_no_device_is_a_record_failure(self):
        with mock.patch.object(capture.shutil, "which",
                               return_value="/usr/bin/adb"), \
                mock.patch.object(capture, "adb",
                                  return_value=mock.Mock(returncode=1)):
            code, out, _ = run_main(capture, ["start", "--dir", self.dir, "--json"])
        data = json.loads(out)
        self.assertEqual(code, 1)
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "no-device")

    def test_no_adb_is_a_missing_prerequisite(self):
        with mock.patch.object(capture.shutil, "which", return_value=None):
            code, out, _ = run_main(capture, ["start", "--dir", self.dir, "--json"])
        data = json.loads(out)
        self.assertEqual(code, 3)
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "no-adb")

    def sidecar(self):
        sidecar = {"recording": "recording.mp4", "source": "screenrecord",
                   "size": 4, "duration": 0.1,
                   "sync": {"clock": "elapsed", "unit_ns": 1_000_000_000,
                            "frame_count": 0},
                   "frames": [],
                   "events": [{"name": "session:typed", "host": 1.0, "frame": 0,
                               "video_s": 0.0, "since_frame_ms": 0.0}]}
        with open(os.path.join(self.dir, "recording.json"), "w") as handle:
            json.dump(sidecar, handle)
        with open(os.path.join(self.dir, "recording.mp4"), "wb") as handle:
            handle.write(b"mp4\n")
        return sidecar

    def test_show_json_is_the_sidecar_with_a_schema(self):
        self.sidecar()
        code, out, err = run_main(capture, ["show", "--dir", self.dir, "--json"])
        data = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(data["schema"], "capture-recording")
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["recording"], "recording.mp4")
        self.assertEqual(data["events"][0]["name"], "session:typed")

    def test_show_json_written_back_is_accepted_by_frames(self):
        self.sidecar()
        _, out, _ = run_main(capture, ["show", "--dir", self.dir, "--json"])
        shown = os.path.join(self.dir, "shown.json")
        with open(shown, "w") as handle:
            handle.write(out)
        frames = os.path.join(self.dir, "frames")
        with mock.patch.object(capture.shutil, "which",
                               return_value="/usr/bin/ffmpeg"), \
                mock.patch.object(capture.subprocess, "run",
                                  return_value=mock.Mock(returncode=0)) as ffmpeg:
            code, stdout, err = run_main(
                capture, ["frames", "--dir", self.dir, "--sidecar", shown,
                          "--event", "session:typed", "--out", frames])
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        self.assertEqual(ffmpeg.call_count, 1)
        self.assertIn("session:typed", stdout)

    def test_frames_without_ffmpeg_is_a_missing_prerequisite(self):
        with mock.patch.object(capture.shutil, "which", return_value=None):
            code, _, err = run_main(capture, ["frames", "--dir", self.dir])
        self.assertEqual(code, 3)
        self.assertIn("ffmpeg is not in PATH", err)

    def test_frames_without_a_pick_is_a_usage_error(self):
        self.sidecar()
        with mock.patch.object(capture.shutil, "which",
                               return_value="/usr/bin/ffmpeg"):
            code, _, err = run_main(capture, ["frames", "--dir", self.dir])
        self.assertEqual(code, 2)
        self.assertIn("say what to extract", err)

    def test_no_verb_is_a_usage_error(self):
        code, _, _ = run_main(capture, [])
        self.assertEqual(code, 2)

    def test_version_is_printed(self):
        code, out, _ = run_main(capture, ["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "capture %s" % capture.__version__)

    def test_version_after_the_verb_is_printed(self):
        code, out, _ = run_main(capture, ["show", "--version"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "capture %s" % capture.__version__)


class WatchShellTest(unittest.TestCase):
    """The watcher's status object and its state-change stream."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.out = self.directory.name
        self.bin = os.path.join(self.out, "bin")
        os.makedirs(self.bin)
        self.write_executable("adb", """#!/bin/sh
case "$1" in
    devices)
        printf 'List of devices attached\\n'
        for d in ${FAKE_DEVICES:-}; do printf '%s\\tdevice\\n' "$d"; done
        ;;
    *) : ;;
esac
""")
        self.script = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "adb_harness", "watch.sh")

    def write_executable(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w") as handle:
            handle.write(body)
        os.chmod(path, 0o755)
        return path

    def run_watch(self, *argv, devices=()):
        env = dict(os.environ)
        env["ADB_WATCH_OUT"] = self.out
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        env["ADB_HARNESS_PYTHON"] = sys.executable
        env["FAKE_DEVICES"] = " ".join(devices)
        return subprocess.run(["bash", self.script, *argv], capture_output=True,
                              text=True, env=env, timeout=30)

    def status(self, running):
        path = os.path.join(self.out, "adb-watch.pid")
        if running:
            with open(path, "w") as handle:
                handle.write("%d\n" % os.getpid())
        elif os.path.exists(path):
            os.remove(path)

    def test_status_json_when_running(self):
        self.status(True)
        with open(os.path.join(self.out, "adb-watch.attempts"), "w") as handle:
            handle.write("3\n")
        result = self.run_watch("--status", "--json",
                                devices=["127.0.0.1:38109", "emulator-5554"])
        data = json.loads(result.stdout)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(data["schema"], "adb-watch")
        self.assertEqual(data["version"], 1)
        self.assertTrue(data["running"])
        self.assertEqual(data["pid"], os.getpid())
        self.assertEqual(data["devices"], ["127.0.0.1:38109", "emulator-5554"])
        self.assertEqual(data["attempts"], 3)

    def test_status_json_when_not_running(self):
        result = self.run_watch("--status", "--json")
        data = json.loads(result.stdout)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(data["running"])
        self.assertIsNone(data["pid"])
        self.assertEqual(data["devices"], [])
        self.assertEqual(data["attempts"], 0)

    def test_human_status_is_unchanged(self):
        self.status(True)
        result = self.run_watch("--status", devices=["127.0.0.1:38109"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout,
                         "adb-watch running (pid %d)\n"
                         "device: 127.0.0.1:38109\n" % os.getpid())

    def test_human_status_when_not_running_is_unchanged(self):
        result = self.run_watch("--status")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "adb-watch not running\n")

    def test_streaming_json_has_one_object_per_state_change(self):
        self.write_executable("adb-connect", """#!/bin/sh
echo "nothing advertised _adb-tls-connect._tcp.local." >&2
exit 1
""")
        result = self.run_watch("--json", "--quiet", "--interval", "1",
                                "--max-attempts", "1")
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(lines), 1)
        data = json.loads(lines[0])
        self.assertEqual(data["schema"], "adb-watch")
        self.assertEqual(data["event"], "error")
        self.assertEqual(data["attempts"], 1)
        self.assertIn("nothing advertised", data["detail"])

    def test_an_unknown_flag_is_a_usage_error(self):
        self.assertEqual(self.run_watch("--wat").returncode, 2)

    def test_a_bad_interval_is_a_usage_error(self):
        self.assertEqual(self.run_watch("--interval", "soon").returncode, 2)

    def test_version_is_printed(self):
        code, out, _ = run_main(watch, ["--version"])
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "adb-watch %s" % watch.__version__)


if __name__ == "__main__":
    unittest.main()
