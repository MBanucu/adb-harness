"""Find a phone's wireless-debugging adb port, keep it connected, and record its
screen so a moment in a run can be looked at afterwards.

Three commands, installed together:

    capture       one screenrecord for a run, an event beside each moment, and
                  the exact frame for an event
    adb-connect   the mDNS lookup for the wireless-debugging port, and the
                  `adb connect` that follows it
    adb-watch     a loop that runs adb-connect whenever the device list goes
                  empty

Nothing here is a library: the modules are the commands' implementations and the
shell script the watcher runs.
"""

__version__ = "0.2.2"
