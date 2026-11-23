"""The exceptions the library raises, and the one the capture falls back on.

The three commands catch these and turn them into the exit codes and JSON
objects documented in the README; a caller that imports the modules gets the
exceptions instead of a process that exits.

    NoAdb          adb is not in PATH
    NoMdns         python-zeroconf is missing, so `connect.find` cannot ask mDNS
    NotConnected   no device answered: nothing advertised, a stale port, or no
                   device in the `device` state
    CaptureFailed  a recording could not start, stop, or be mapped
    SourceUnavailable  one way of recording cannot run here; `auto` falls back
"""


class HarnessError(Exception):
    """What every exception in this module has in common."""


class NoAdb(HarnessError):
    """adb is not in PATH, so nothing can be asked of a device."""


class NoMdns(HarnessError):
    """python-zeroconf is missing; `connect.find` has no way to ask mDNS."""


class NotConnected(HarnessError):
    """No device answered, or the one asked for is not in the `device` state."""

    def __init__(self, message, port=None, said=""):
        super().__init__(message)
        self.port = port
        self.said = said


class CaptureFailed(HarnessError):
    """A recording could not start, could not be stopped, or could not be mapped."""


class SourceUnavailable(HarnessError):
    """A way of recording cannot run here, and `auto` should try screenrecord."""
