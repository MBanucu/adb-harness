"""Find a phone's wireless-debugging adb port, keep it connected, and record its
screen so a moment in a run can be looked at afterwards.

Three commands are installed together:

    capture       one screenrecord for a run, an event beside each moment, and
                  the exact frame for an event
    adb-connect   the mDNS lookup for the wireless-debugging port, and the
                  `adb connect` that follows it
    adb-watch     a loop that runs adb-connect whenever the device list goes
                  empty

The same work is a library, without printing or shelling out:

    from adb_harness import capture, connect

    port = connect.find(timeout=8)      # Port | None
    result = connect.connect(port)      # ConnectResult(serial, address, already)

    with capture.Session(dir="out/capture", serial=result.serial) as run:
        run.event("check:diag")         # no process, no adb call
        frames = run.stop()             # Recording(video, sidecar, events)
        frame = frames.frame_for("check:diag")

`watch.Watcher` is the reconnect loop as an object with `.run()`, `.stop()` and
`.status()`. The exceptions are in `errors.py`; the commands catch them and turn
them into the documented exit codes, while a library caller sees the exception.
"""

import importlib

from .errors import (CaptureFailed, HarnessError, NoAdb, NoMdns,
                     NotConnected, SourceUnavailable)

__version__ = "0.2.3"

__all__ = [
    "capture",
    "connect",
    "watch",
    "HarnessError",
    "NoAdb",
    "NoMdns",
    "NotConnected",
    "CaptureFailed",
    "SourceUnavailable",
]

_MODULES = {"capture", "connect", "watch"}


def __getattr__(name):
    """The three modules, imported on first use.

    Importing them here would make `python3 -m adb_harness.capture` find the
    module already loaded, which runpy warns about; a lazy lookup keeps both the
    `from adb_harness import capture` form and `-m` clean.
    """
    if name in _MODULES:
        return importlib.import_module("." + name, __name__)
    raise AttributeError("module %r has no attribute %r" % (__name__, name))
