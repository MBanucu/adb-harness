"""The adb calls and the two clocks, shared by the capture and its sources."""
import os
import subprocess
import time


def adb(args, serial="", capture=True):
    command = [os.environ.get("ADB", "adb")]
    if serial:
        command += ["-s", serial]
    command += args
    if capture:
        return subprocess.run(command, capture_output=True, text=True)
    return subprocess.run(command)


def device_shell(command, serial=""):
    return adb(["shell", command], serial)


def host_clock():
    """This machine's monotonic seconds: a clock ntpd cannot step.

    Termux cannot read /proc/uptime (SELinux), and time.monotonic is the same
    kind of clock anyway. It is not the device's boot clock: the difference is
    the machine's own suspend time, a constant for the run, and it lands in the
    measured offset rather than in the mapping.
    """
    return time.monotonic()


def device_uptime(serial=""):
    """The device's elapsed-realtime seconds, the clock the video's frames use."""
    result = device_shell("cat /proc/uptime", serial)
    if result.returncode != 0:
        return None
    return float(result.stdout.split()[0])


def sample_clock(serial=""):
    """(device - host) in seconds, with the half round trip as its error bar."""
    before = host_clock()
    device = device_uptime(serial)
    after = host_clock()
    if device is None:
        return None
    # The device's reading was taken somewhere inside the call, so the middle of
    # the call is the best guess at when, and half the call is how wrong that is.
    return {"offset": device - (before + after) / 2.0, "error": (after - before) / 2.0}
