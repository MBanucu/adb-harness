"""Tests for `connect` discovery and connection without a device.

`tests/fakeadb.py` puts an `adb` executable first on PATH, so `attached`, `adb`
and the stale-port path run as they do on a phone, and only the process at the
other end is synthetic. The mDNS tests feed `discover`'s real callback path with
`zeroconf.ServiceInfo` objects and a browser that answers immediately; no socket
is opened. If the optional `zeroconf` extra is not installed those tests skip
with the reason, and the fake-adb tests still run.
"""

import io
import json
import os
import socket
import sys
import tempfile
import time
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from adb_harness import connect  # noqa: E402
from adb_harness.errors import NotConnected  # noqa: E402
from fakeadb import FakeAdb  # noqa: E402

try:
    import zeroconf
except ImportError:  # the optional extra; the fake-adb tests do not need it
    zeroconf = None


def run_main(module, argv):
    """The command's return code, with both streams kept for inspection."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        try:
            code = module.main(argv)
        except SystemExit as stopped:
            code = stopped.code if isinstance(stopped.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def service_info(type_, name, port, *addresses):
    """A real ServiceInfo, as zeroconf would hand to the listener."""
    addresses = addresses or ("127.0.0.1",)
    return zeroconf.ServiceInfo(
        type_, name,
        addresses=[socket.inet_aton(address) for address in addresses],
        port=port,
        properties={},
        server=name.split(".")[0] + ".local.")


def fake_mdns(services):
    """Patch zeroconf's constructors so `discover` sees `services` at once.

    `services` maps a service type to the `ServiceInfo` objects advertised for
    it. The returned list collects the fake Zeroconf instances so a test can
    check that `discover` closed the one it opened.
    """
    opened = []

    class FakeZeroconf:
        def __init__(self):
            self.closed = False
            opened.append(self)

        def get_service_info(self, type_, name, timeout=2000):
            for info in services.get(type_, []):
                if info.name == name:
                    return info
            return None

        def close(self):
            self.closed = True

    class FakeServiceBrowser:
        def __init__(self, zc, type_, listener):
            for info in services.get(type_, []):
                listener.add_service(zc, type_, info.name)

    patcher = mock.patch.multiple(
        zeroconf, Zeroconf=FakeZeroconf, ServiceBrowser=FakeServiceBrowser)
    return patcher, opened


class FakeAdbConnectTest(unittest.TestCase):
    """`attached` and `adb` through an executable rather than a Mock."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fake = FakeAdb(self.directory.name, {
            "devices": [["127.0.0.1:38109", "device"],
                        ["emulator-5554", "offline"],
                        ["unauthorised-1", "unauthorized"]],
            "other": {"output": "stdout line", "stderr": "stderr line", "code": 0},
        })
        self.env = mock.patch.dict(os.environ, self.fake.environment())
        self.env.start()
        self.addCleanup(self.env.stop)

    def run_connect(self, argv):
        """The command, with zeroconf only present enough to get past its check.

        These cases answer from the fake adb before discovery is reached; they
        must not need the optional extra just to test `--port` or `--serial`.
        """
        with mock.patch.dict(sys.modules, {"zeroconf": types.ModuleType("zeroconf")}):
            return run_main(connect, argv)

    def test_attached_keeps_only_the_device_state(self):
        self.assertEqual(connect.attached(), ["127.0.0.1:38109"])

    def test_adb_returns_stdout_and_stderr_together(self):
        # The callers branch on text adb writes to either stream.
        self.assertEqual(connect.adb("version"), "stdout line\nstderr line")

    def test_an_attached_device_is_not_connected_again(self):
        self.fake.reset({"devices": [["127.0.0.1:38109", "device"]]})
        port = connect.find()
        self.assertEqual(port.source, "attached")
        self.assertEqual(port.serial, "127.0.0.1:38109")
        self.assertEqual([call[1] for call in self.fake.calls("adb")], ["devices"])

    def test_the_named_serial_wins_when_two_are_attached(self):
        self.fake.reset({"devices": [["A", "device"], ["B", "device"]]})
        self.assertEqual(connect.find().serial, "A")
        self.assertEqual(connect.find(serial="B").serial, "B")
        code, out, err = self.run_connect(["--json", "--serial", "B"])
        data = json.loads(out)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(data["serial"], "B")
        self.assertEqual(data["source"], "attached")
        self.assertEqual(data["port"], 0)  # a serial that is not host:port
        self.assertEqual([call[1] for call in self.fake.calls("adb")],
                         ["devices", "devices", "devices"])

    def test_a_named_port_that_nothing_answers_is_stale_port(self):
        self.fake.reset({"devices": [],
                         "connect_default": {
                             "output": "failed to connect: Connection refused",
                             "code": 1}})
        code, out, err = self.run_connect(["--json", "--port", "9999"])
        data = json.loads(out)
        self.assertEqual((code, err), (1, ""))
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "stale-port")
        self.assertEqual(data["port"], 9999)
        self.assertIn("Connection refused", data["detail"])
        self.assertIn(["adb", "connect", "127.0.0.1:9999"],
                      self.fake.calls("adb"))
        self.assertEqual(self.fake.calls("adb")[-1], ["adb", "devices"])


@unittest.skipUnless(
    zeroconf is not None,
    "python-zeroconf is not installed; pip install 'adb-harness[mdns]'")
class MdnsDiscoveryTest(unittest.TestCase):
    """`discover` and the commands over constructed zeroconf objects."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.fake = FakeAdb(self.directory.name, {"devices": []})
        self.env = mock.patch.dict(os.environ, self.fake.environment())
        self.env.start()
        self.addCleanup(self.env.stop)

    def connect_info(self, port=38109, name="phone._adb-tls-connect._tcp.local."):
        return service_info(connect.CONNECT_SERVICES[0], name, port)

    def pairing_info(self, port=39123, name="phone._adb-tls-pairing._tcp.local."):
        return service_info(connect.PAIRING_SERVICE, name, port)

    def discover(self, services, seconds=0.05):
        patcher, opened = fake_mdns(services)
        with patcher, mock.patch.object(connect, "own_addresses", return_value=[]):
            found, pairing = connect.discover(seconds)
        return found, pairing, opened

    def test_the_connect_service_wins_over_the_pairing_one(self):
        # The guard for the preference: if pairing were chosen here, the port
        # and address assertions below would name 39123 rather than 38109.
        info = self.connect_info()
        pairing = self.pairing_info()
        patcher, _opened = fake_mdns({
            connect.CONNECT_SERVICES[0]: [info],
            connect.PAIRING_SERVICE: [pairing],
        })
        with patcher, mock.patch.object(connect, "own_addresses", return_value=[]):
            found, pairings = connect.discover(0.05)
            port = connect.find(timeout=0.05)
        self.assertEqual([item.port for item in found], [38109])
        self.assertEqual([item.port for item in pairings], [39123])
        self.assertEqual(port.port, 38109)
        self.assertEqual(port.address, "127.0.0.1:38109")
        self.assertNotEqual(port.port, pairing.port)

    def test_the_pair_flag_keeps_using_the_pairing_service(self):
        patcher, _opened = fake_mdns({
            connect.PAIRING_SERVICE: [self.pairing_info()],
        })
        with patcher, mock.patch.object(connect, "own_addresses", return_value=[]):
            code, out, err = run_main(connect, ["--pair", "--json",
                                                "--timeout", "0.05"])
        data = json.loads(out)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(data["source"], "pairing")
        self.assertEqual(data["address"], "127.0.0.1:39123")
        self.assertEqual(data["pairing"][0]["command"], "adb pair 127.0.0.1:39123")

    def test_a_phone_advertising_loopback_is_reached_on_loopback(self):
        info = service_info(connect.CONNECT_SERVICES[0],
                            "phone._adb-tls-connect._tcp.local.", 38109,
                            "127.0.0.1", "192.168.1.5")
        with mock.patch.object(connect, "own_addresses",
                               return_value=["192.168.1.5", "10.0.0.2"]):
            # The advertised address and this phone's own are the same machine,
            # so loopback stays first and the duplicate is dropped.
            self.assertEqual(connect.addresses(info),
                             ["127.0.0.1", "192.168.1.5", "10.0.0.2"])
            port = connect.port_for(info)
        self.assertEqual(port.address, "127.0.0.1:38109")
        self.assertEqual(port.candidates[0], "127.0.0.1:38109")
        self.assertEqual(len(port.candidates), 3)

    def test_nothing_advertised_within_the_timeout(self):
        started = time.monotonic()
        found, pairing, opened = self.discover({}, seconds=0.05)
        elapsed = time.monotonic() - started
        self.assertEqual((found, pairing), ([], []))
        self.assertLess(elapsed, 1.0)
        self.assertTrue(opened and opened[0].closed)

    def test_two_advertised_phones_use_the_one_that_answers(self):
        first = self.connect_info(38109, "a-phone._adb-tls-connect._tcp.local.")
        second = self.connect_info(38110, "b-phone._adb-tls-connect._tcp.local.")
        self.fake.reset({
            "devices": [],
            "connect": {
                "127.0.0.1:38110": {
                    "output": "connected to 127.0.0.1:38110", "code": 0,
                    "add": ["127.0.0.1:38110"]},
            },
            "connect_default": {
                "output": "failed to connect: Connection refused", "code": 1},
        })
        patcher, _opened = fake_mdns({
            connect.CONNECT_SERVICES[0]: [first, second],
        })
        with patcher, mock.patch.object(connect, "own_addresses", return_value=[]):
            code, out, err = run_main(connect, ["--json"])
        data = json.loads(out)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(data["address"], "127.0.0.1:38110")
        self.assertEqual(data["port"], 38110)
        # Both were tried, in discovery order, and the one that answered won.
        attempts = [call[2] for call in self.fake.calls("adb")
                    if len(call) > 2 and call[1] == "connect"]
        self.assertEqual(attempts, ["127.0.0.1:38109", "127.0.0.1:38110"])

    def test_a_stale_advertisement_is_stale_port_json(self):
        info = self.connect_info()
        self.fake.reset({"devices": [], "connect_default": {
            "output": "failed to connect: Connection refused", "code": 1}})
        patcher, _opened = fake_mdns({connect.CONNECT_SERVICES[0]: [info]})
        with patcher, mock.patch.object(connect, "own_addresses", return_value=[]):
            code, out, err = run_main(connect, ["--json"])
        data = json.loads(out)
        self.assertEqual((code, err), (1, ""))
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "stale-port")
        self.assertEqual(data["port"], 38109)
        self.assertIn("127.0.0.1:38109", data["detail"])
        self.assertIn("Connection refused", data["detail"])

    def test_a_stale_advertisement_says_what_to_do_in_prose(self):
        info = self.connect_info()
        self.fake.reset({"devices": [], "connect_default": {
            "output": "failed to connect: Connection refused", "code": 1}})
        patcher, _opened = fake_mdns({connect.CONNECT_SERVICES[0]: [info]})
        with patcher, mock.patch.object(connect, "own_addresses", return_value=[]):
            code, out, err = run_main(connect, [])
        self.assertEqual(code, 1)
        self.assertIn("nothing answers on it", err)
        self.assertIn("Wireless debugging off and on", err)

    def test_nothing_advertised_is_no_service_json(self):
        patcher, _opened = fake_mdns({})
        with patcher:
            code, out, err = run_main(connect, ["--json", "--timeout", "0.05"])
        data = json.loads(out)
        self.assertEqual((code, err), (1, ""))
        self.assertFalse(data["ok"])
        self.assertEqual(data["reason"], "no-service")
        self.assertIsNone(data["port"])
        self.assertIn("nothing advertised", data["detail"])

    def test_connect_raises_not_connected_with_the_last_adb_words(self):
        info = self.connect_info()
        self.fake.reset({"devices": [], "connect_default": {
            "output": "failed to connect: Connection refused", "code": 1}})
        patcher, _opened = fake_mdns({connect.CONNECT_SERVICES[0]: [info]})
        with patcher, mock.patch.object(connect, "own_addresses", return_value=[]):
            port = connect.find(timeout=0.05)
            with self.assertRaises(NotConnected) as caught:
                connect.connect(port)
        self.assertEqual(caught.exception.port, 38109)
        self.assertIn("Connection refused", caught.exception.said)


if __name__ == "__main__":
    unittest.main()
