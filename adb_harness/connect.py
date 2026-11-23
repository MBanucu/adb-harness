#!/usr/bin/env python3
"""Finds this phone's wireless-debugging port over mDNS and connects adb to it.

Android advertises wireless debugging as `_adb-tls-connect._tcp` for as long as
the setting is on, and the port changes every time it is turned off and on
again - which is why an adb session that worked yesterday finds nothing today.
Termux's adb cannot answer that question itself (`adb mdns services` says "mdns
is not supported by this version of adb"), and `ss` and `/proc/net/tcp6` are
closed to an app, so python-zeroconf does the lookup and this script hands the
answer to `adb connect`.

The command is the supported interface for a shell:

    adb-connect               # discover, connect, say which port
    adb-connect --pair        # same lookup, for the pairing port
    adb-connect --port 38109  # skip discovery

The same work is available without printing, for a test suite:

    from adb_harness import connect

    port = connect.find(timeout=8)      # Port | None
    result = connect.connect(port)      # ConnectResult(serial, address, already)

`find` returns a `Port` rather than printing, and `connect` returns where adb
ended up rather than formatting it. Both raise the exceptions in `errors.py`:
`NoAdb`, `NoMdns` and `NotConnected`.

Needs adb in PATH, python3 with zeroconf (`pip install zeroconf`), and Wireless
debugging on in Developer options. A phone that has never been paired with this
adb key has to be paired once, by hand, with the code it shows:

    adb-connect --pair    # prints the command, then paste the code
"""

import argparse
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass

from . import __version__
from .errors import NoAdb, NoMdns, NotConnected
from .output import EXIT_FAILURE, EXIT_PREREQUISITE, emit

# What a phone with Wireless debugging on advertises. `_adb._tcp` is the older
# (pre-TLS) discovery record; asking for both costs nothing and covers a phone
# that still publishes it.
CONNECT_SERVICES = ("_adb-tls-connect._tcp.local.", "_adb._tcp.local.")
PAIRING_SERVICE = "_adb-tls-pairing._tcp.local."

SCHEMA = "adb-connect"
VERSION = "adb-connect %s" % __version__


@dataclass(frozen=True)
class Port:
    """A wireless-debugging port, and where adb should try to reach it.

    `address` is the `host:port` handed to `adb connect`; `port` is the number on
    its own, as the JSON contract and `adb-connect --json` report it. `source` is
    `mdns` (found by a lookup), `port` (named with `--port`) or `attached` (adb
    already had a device). `serial` is the serial to prefer when more than one
    device is attached; `candidates` are the addresses to try, loopback first.
    """

    address: str
    port: int
    serial: str = ""
    source: str = "mdns"
    name: str = ""
    candidates: tuple[str, ...] = ()

    @classmethod
    def from_serial(cls, serial):
        """The port of a serial adb already has, as `source: attached`."""
        return cls(address=serial, port=port_of(serial), serial=serial,
                   source="attached")


@dataclass(frozen=True)
class ConnectResult:
    """What `adb connect` produced: the serial, the address, and whether it was
    already there (`already`) rather than connected by this call."""

    serial: str
    address: str
    already: bool = False
    port: int = 0
    source: str = "mdns"
    name: str = ""
    devices: tuple[str, ...] = ()
    detail: str = ""


def adb(*args):
    """Runs one adb command and returns its combined output."""
    result = subprocess.run(["adb", *args], capture_output=True, text=True, timeout=30)
    return (result.stdout + result.stderr).strip()


def attached():
    """The serials adb already has in the `device` state, in order."""
    serials = []
    for line in adb("devices").splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device":
            serials.append(fields[0])
    return serials


def require_adb():
    """Raises `NoAdb` when adb is not in PATH."""
    if shutil.which("adb") is None:
        raise NoAdb("adb is not in PATH")


def discover(seconds):
    """Browses mDNS and returns (connect services, pairing services).

    Raises `NoMdns` when python-zeroconf is not installed.
    """
    try:
        from zeroconf import ServiceBrowser, ServiceListener, Zeroconf
    except ImportError as why:
        raise NoMdns("python-zeroconf is missing: pip install zeroconf") from why

    found = []
    pairing = []

    class Listener(ServiceListener):
        # Adding, updating and removing are all the same question here: is
        # there a service, and what port does it name?
        def __init__(self, into):
            self.into = into

        def _take(self, zc, type_, name):
            info = zc.get_service_info(type_, name, timeout=2000)
            if info is None or info.port is None:
                return
            self.into[:] = [other for other in self.into if other[0] != name]
            self.into.append((name, info))

        def add_service(self, zc, type_, name):
            self._take(zc, type_, name)

        def update_service(self, zc, type_, name):
            self._take(zc, type_, name)

        def remove_service(self, zc, type_, name):
            self.into[:] = [other for other in self.into if other[0] != name]

    zc = Zeroconf()
    try:
        for type_ in CONNECT_SERVICES:
            ServiceBrowser(zc, type_, Listener(found))
        ServiceBrowser(zc, PAIRING_SERVICE, Listener(pairing))
        deadline = time.time() + seconds
        # The answer normally arrives within a second; the first lookup of the
        # run is sometimes slower, which is what the rest of the budget is for.
        while time.time() < deadline and not found:
            time.sleep(0.1)
        return [info for _, info in found], [info for _, info in pairing]
    finally:
        zc.close()


def own_addresses():
    """This phone's own IPv4 addresses, for a listener bound to Wi-Fi alone."""
    found = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # Connecting a datagram socket sends nothing: it only asks the kernel
        # which interface a route would leave by, and reads that address back.
        probe.connect(("8.8.8.8", 53))
        found.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()
    return found


def addresses(info):
    """Where to reach an advertised service, loopback first.

    On this phone the advertiser is the device adb is about to talk to, so
    127.0.0.1 is the same machine without the Wi-Fi round trip. The advertised
    addresses follow for the case where this runs on a laptop, and this phone's
    own come last: adbd sometimes listens on the Wi-Fi address only.
    """
    try:
        advertised = info.parsed_scoped_addresses()
    except AttributeError:  # older zeroconf
        advertised = info.parsed_addresses()
    candidates = ["127.0.0.1", *advertised, *own_addresses()]
    return list(dict.fromkeys(candidates))


def target(address, port):
    """`host:port`, with an IPv6 address wrapped the way adb wants it."""
    return f"[{address}]:{port}" if ":" in address else f"{address}:{port}"


def port_for(info, serial=""):
    """The `Port` an advertised mDNS service stands for."""
    candidates = tuple(target(address, info.port) for address in addresses(info))
    address = candidates[0] if candidates else target("127.0.0.1", info.port)
    return Port(address=address, port=info.port, serial=serial, source="mdns",
                name=getattr(info, "name", ""), candidates=candidates)


def find(timeout=8.0, *, port=None, serial=""):
    """The wireless-debugging port, or None when nothing is advertised.

    A device already in adb's `device` state is returned with `source
    `"attached"` without asking mDNS; `port` skips discovery and names loopback.
    `serial` is the serial to prefer when more than one device is attached.

    Raises `NoAdb` when adb is missing and `NoMdns` when python-zeroconf is.
    """
    require_adb()
    if port is not None:
        where = target("127.0.0.1", port)
        return Port(address=where, port=port, serial=serial, source="port",
                    candidates=(where,))
    devices = attached()
    if devices:
        chosen = serial if serial in devices else devices[0]
        return Port.from_serial(chosen)
    found, _pairing = discover(timeout)
    if not found:
        return None
    return port_for(found[0], serial)


def connect(port):
    """Runs `adb connect` for a `Port` and returns a `ConnectResult`.

    Every candidate address is tried until adb reports a device. A `Port` with
    `source="attached"` is already connected and returns `already=True` without
    touching adb. Raises `NotConnected` when no address produces a device; the
    exception carries the port and the last thing adb said.
    """
    if port.source == "attached":
        serial = port.serial or port.address
        return ConnectResult(serial=serial, address=port.address, already=True,
                             port=port.port or port_of(serial),
                             source="attached", devices=(serial,))
    candidates = port.candidates or (port.address,)
    where, said = port.address, ""
    for where in candidates:
        said = adb("connect", where)
        devices = attached()
        if devices:
            serial = (port.serial if port.serial in devices
                      else where if where in devices else devices[0])
            return ConnectResult(serial=serial, address=where, already=False,
                                 port=port.port or port_of(where),
                                 source=port.source, name=port.name,
                                 devices=tuple(devices), detail=said)
    raise NotConnected(f"{where}: {said}", port=port.port, said=said)


def port_of(serial):
    """The port in `host:port`, or 0 for a serial that is not an address."""
    _, _, tail = serial.rpartition(":")
    return int(tail) if tail.isdigit() else 0


def failure(args, reason, detail, port=None, code=EXIT_FAILURE):
    """The failure shape: one object on stdout when asked for JSON."""
    if args.json:
        emit(SCHEMA, ok=False, reason=reason, detail=detail, port=port)
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Find the wireless-debugging port over mDNS and connect adb.")
    parser.add_argument("--timeout", type=float, default=8.0,
                        help="seconds to wait for an advertisement (default 8)")
    parser.add_argument("--port", type=int,
                        help="connect to 127.0.0.1 at this port instead of asking mDNS")
    parser.add_argument("--serial", default="",
                        help="serial to prefer when more than one device is attached")
    parser.add_argument("--pair", action="store_true",
                        help="print the pairing command instead of connecting")
    parser.add_argument("--json", action="store_true",
                        help="print one JSON object on stdout instead of prose")
    parser.add_argument("--quiet", action="store_true",
                        help="no progress lines; the final output is still printed")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)

    if shutil.which("adb") is None:
        if args.json:
            return failure(args, "no-adb", "adb is not in PATH",
                           code=EXIT_PREREQUISITE)
        print("adb is not in PATH", file=sys.stderr)
        return EXIT_PREREQUISITE
    try:
        import zeroconf  # noqa: F401
    except ImportError:
        if args.json:
            return failure(args, "no-mdns",
                           "python-zeroconf is missing: pip install zeroconf",
                           code=EXIT_PREREQUISITE)
        print("python-zeroconf is missing: pip install zeroconf", file=sys.stderr)
        return EXIT_PREREQUISITE

    if args.port is None:
        devices = attached()
        if devices and not args.pair:
            chosen = args.serial if args.serial in devices else devices[0]
            result = connect(Port.from_serial(chosen))
            if args.json:
                emit(SCHEMA, ok=True, address=result.address, port=result.port,
                     serial=result.serial, source="attached", waited_s=0.0)
            else:
                print(f"already connected: {', '.join(devices)}")
            return 0

        if not (args.json or args.quiet):
            print(f"asking mDNS for the wireless-debugging port ({args.timeout:g}s)…")
        started = time.monotonic()
        found, pairing = discover(args.timeout)
        waited = round(time.monotonic() - started, 3)

        if args.pair:
            if not pairing:
                if args.json:
                    return failure(args, "no-service",
                                   "no pairing service advertised")
                print("no pairing service advertised. Turn Wireless debugging on,")
                print("then tap \"Pair device with pairing code\" and try again.")
                return EXIT_FAILURE
            targets = [target(addresses(info)[0], info.port) for info in pairing]
            if args.json:
                emit(SCHEMA, ok=True, address=targets[0],
                     port=port_of(targets[0]), serial="", source="pairing",
                     waited_s=waited,
                     pairing=[{"address": where, "command": f"adb pair {where}"}
                              for where in targets])
            else:
                for where in targets:
                    print(f"adb pair {where}")
            return 0

        if not found:
            if args.json:
                return failure(
                    args, "no-service",
                    "nothing advertised " + " or ".join(CONNECT_SERVICES))
            print("nothing advertised " + " or ".join(CONNECT_SERVICES), file=sys.stderr)
            print("Wireless debugging is off, or its port was not picked up yet.", file=sys.stderr)
            if pairing:
                for info in pairing:
                    print(f"the phone is pairing: adb pair "
                          f"{target(addresses(info)[0], info.port)}", file=sys.stderr)
            return EXIT_FAILURE

        # A phone paired once usually advertises one service; a stale record for
        # an old port can still be around, so every candidate is tried.
        detail = ""
        for info in found:
            try:
                result = connect(port_for(info, args.serial))
            except NotConnected as why:
                detail = str(why)
                if not args.json:
                    print(detail, file=sys.stderr)
                continue
            if args.json:
                emit(SCHEMA, ok=True, address=result.address, port=info.port,
                     serial=result.serial, source="mdns", waited_s=waited)
            else:
                print(f"connected to {result.address}")
                print(f"  advertised as {info.name}")
                for serial in result.devices:
                    print(f"  device: {serial}")
            return 0
        if args.json:
            return failure(args, "stale-port",
                           detail or "the advertised port did not answer",
                           port=found[-1].port if found else None)
        print(file=sys.stderr)
        print("The phone advertises a port, but nothing answers on it. Wireless",
              file=sys.stderr)
        print("debugging is probably off: its mDNS record outlives the setting.",
              file=sys.stderr)
        print("Turn Wireless debugging off and on in Developer options, then",
              file=sys.stderr)
        print("run this again.", file=sys.stderr)
        return EXIT_FAILURE

    where = target("127.0.0.1", args.port)
    port = Port(address=where, port=args.port, serial=args.serial, source="port",
                candidates=(where,))
    try:
        result = connect(port)
    except NotConnected as why:
        if not (args.json or args.quiet):
            print(why.said or str(why))
        if args.json:
            return failure(args, "stale-port", str(why), port=args.port)
        return EXIT_FAILURE
    if not (args.json or args.quiet):
        print(result.detail)
    if args.json:
        emit(SCHEMA, ok=True, address=result.address, port=args.port,
             serial=result.serial, source="port", waited_s=0.0)
    else:
        print(f"connected to {result.address}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
