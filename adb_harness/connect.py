#!/usr/bin/env python3
"""Finds this phone's wireless-debugging port over mDNS and connects adb to it.

Android advertises wireless debugging as `_adb-tls-connect._tcp` for as long as
the setting is on, and the port changes every time it is turned off and on
again - which is why an adb session that worked yesterday finds nothing today.
Termux's adb cannot answer that question itself (`adb mdns services` says "mdns
is not supported by this version of adb"), and `ss` and `/proc/net/tcp6` are
closed to an app, so python-zeroconf does the lookup and this script hands the
answer to `adb connect`.

    adb-connect               # discover, connect, say which port
    adb-connect --pair        # same lookup, for the pairing port
    adb-connect --port 38109  # skip discovery

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

from . import __version__
from .output import EXIT_FAILURE, EXIT_PREREQUISITE, emit

# What a phone with Wireless debugging on advertises. `_adb._tcp` is the older
# (pre-TLS) discovery record; asking for both costs nothing and covers a phone
# that still publishes it.
CONNECT_SERVICES = ("_adb-tls-connect._tcp.local.", "_adb._tcp.local.")
PAIRING_SERVICE = "_adb-tls-pairing._tcp.local."

SCHEMA = "adb-connect"
VERSION = "adb-connect %s" % __version__


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


def discover(seconds):
    """Browses mDNS and returns (connect services, pairing services)."""
    from zeroconf import ServiceBrowser, ServiceListener, Zeroconf

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


def connect(info):
    """Tries the advertised addresses until one is a real adb device.

    <p>Returns the target that worked, or the last one tried and what adb said
    about it, so a refusal names the address it refused.
    """
    where, said = "", ""
    for address in addresses(info):
        where = target(address, info.port)
        said = adb("connect", where)
        if attached():
            return where, said
    return None, f"{where}: {said}"


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
            if args.json:
                serial = devices[0]
                emit(SCHEMA, ok=True, address=serial, port=port_of(serial),
                     serial=serial, source="attached", waited_s=0.0)
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
            where, said = connect(info)
            if where is not None:
                serials = attached()
                serial = where if where in serials else (serials[0] if serials else "")
                if args.json:
                    emit(SCHEMA, ok=True, address=where, port=info.port,
                         serial=serial, source="mdns", waited_s=waited)
                else:
                    print(f"connected to {where}")
                    print(f"  advertised as {info.name}")
                    for serial in serials:
                        print(f"  device: {serial}")
                return 0
            detail = said
            if not args.json:
                print(said, file=sys.stderr)
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
    said = adb("connect", where)
    if not (args.json or args.quiet):
        print(said)
    devices = attached()
    if not devices:
        if args.json:
            return failure(args, "stale-port", f"{where}: {said}",
                           port=args.port)
        return EXIT_FAILURE
    serial = where if where in devices else devices[0]
    if args.json:
        emit(SCHEMA, ok=True, address=where, port=args.port, serial=serial,
             source="port", waited_s=0.0)
    else:
        print(f"connected to {where}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
