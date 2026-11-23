#!/usr/bin/env python3
"""Keeps this phone's wireless-debugging adb connection alive.

The port changes whenever Wireless debugging is turned off and on again, so a
connection that worked earlier simply disappears. The watcher checks the adb
device list, and while there is no device it runs `adb-connect`, which asks mDNS
for the current `_adb-tls-connect._tcp` port and connects to it.

    adb-watch                        # run in the foreground
    adb-watch --status               # is it running, and is a device attached
    adb-watch --status --json        # the same, as one JSON object
    adb-watch --json                 # one JSON object per state change
    adb-watch --interval 5 --max-attempts 10
    kill "$(cat out/adb-watch.pid)"  # stop a running watcher

The loop is `Watcher`, which a test suite can drive in process:

    from adb_harness import watch

    watcher = watch.Watcher(interval=15, max_attempts=0, on_event=print)
    watcher.status()          # WatchStatus(running, pid, devices, attempts)
    watcher.run()             # until stop() or the attempt bound
    watcher.stop()

`adb_harness/watch.sh` is now only a caller of this module; the state, the
attempt count and the reconnect decision are Python.

Environment: ADB_WATCH_INTERVAL (seconds, default 15), ADB_WATCH_OUT (the
directory holding the pid, attempts and log, default out/ in the working
directory) and ADB_WATCH_LOG (default `$ADB_WATCH_OUT/adb-watch.log`).
`--interval` and `--max-attempts` override the interval and bound the number of
reconnect attempts (`0`, the default, is no bound). State changes are logged; a
repeated failure is logged again only when the message changes or ten minutes
have passed, so a phone left with Wireless debugging off does not fill the log.
`--quiet` leaves the log alone; JSON objects still go to stdout. `--serial`
watches one device when more than one is attached.
"""

import argparse
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from . import __version__
from .output import emit

VERSION = "adb-watch %s" % __version__
SCHEMA = "adb-watch"
REPEAT_AFTER = 600


@dataclass(frozen=True)
class WatchEvent:
    """One state change: `connected`, `reconnected`, `disconnected` or `error`."""

    event: str
    devices: tuple = ()
    attempts: int = 0
    serial: str = ""
    detail: str = ""


@dataclass(frozen=True)
class WatchStatus:
    """The watcher's state, in process or read back from the pid and attempts."""

    running: bool
    pid: int | None = None
    devices: tuple = ()
    attempts: int = 0


def output_dir(out=None):
    return out or os.environ.get("ADB_WATCH_OUT") or os.path.join(os.getcwd(), "out")


def log_path(out=None):
    out = output_dir(out)
    return os.environ.get("ADB_WATCH_LOG") or os.path.join(out, "adb-watch.log")


def devices(serial=""):
    """The serials adb reports in the `device` state, optionally just one."""
    try:
        result = subprocess.run(
            [os.environ.get("ADB", "adb"), "devices"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    found = []
    for line in (result.stdout + result.stderr).splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[1] == "device":
            if not serial or fields[0] == serial:
                found.append(fields[0])
    return found


def live_pid(path):
    """The pid in `path` when it names a process that is still running."""
    try:
        with open(path) as handle:
            pid = int(handle.read().strip())
    except (OSError, ValueError):
        return None
    try:
        os.kill(pid, 0)
    except OSError:
        return None
    return pid


def attempt_count(path):
    try:
        with open(path) as handle:
            count = int(handle.read().strip())
    except (OSError, ValueError):
        return 0
    return max(0, count)


def read_status(out=None, serial=""):
    """The status the pidfile and attempts file describe, for `--status`."""
    out = output_dir(out)
    pidfile = os.path.join(out, "adb-watch.pid")
    pid = live_pid(pidfile)
    running = pid is not None
    count = attempt_count(os.path.join(out, "adb-watch.attempts")) if running else 0
    return WatchStatus(running=running, pid=pid if running else None,
                       devices=tuple(devices(serial)), attempts=count)


class Watcher:
    """The reconnect loop, as an object a runner can start, stop and ask about.

    `on_event` receives a `WatchEvent` for every state change; it is where a
    caller puts its log line and its JSON, so the loop itself prints nothing.
    """

    def __init__(self, interval=15, max_attempts=0, on_event=None, *, out=None,
                 serial="", connect_command=None):
        try:
            interval = float(interval)
        except (TypeError, ValueError):
            raise ValueError("intervals are seconds") from None
        if interval <= 0:
            raise ValueError("intervals are seconds")
        try:
            max_attempts = int(max_attempts)
        except (TypeError, ValueError):
            raise ValueError("--max-attempts wants a whole number") from None
        if max_attempts < 0:
            raise ValueError("--max-attempts wants a whole number")
        self.interval = interval
        self.max_attempts = max_attempts
        self.on_event = on_event
        self.out = output_dir(out)
        self.serial = serial
        self.connect_command = tuple(connect_command or ()) or None
        self.pidfile = os.path.join(self.out, "adb-watch.pid")
        self.attempts_file = os.path.join(self.out, "adb-watch.attempts")
        self.running = False
        self.pid = None
        self.attempts = 0
        self.devices_now = ()
        self._stop = threading.Event()

    def status(self):
        """Running state, pid, devices and attempt count as this process knows them."""
        return WatchStatus(running=self.running,
                           pid=self.pid if self.running else None,
                           devices=tuple(self.devices_now),
                           attempts=self.attempts)

    def stop(self):
        """Asks `run` to return at its next wait. Safe from a signal or thread."""
        self._stop.set()

    def _note_attempts(self):
        try:
            with open(self.attempts_file, "w") as handle:
                handle.write("%d\n" % self.attempts)
        except OSError:
            pass

    def _emit(self, event):
        if self.on_event is not None:
            self.on_event(event)

    def _devices(self):
        return devices(self.serial)

    def _adb_start(self):
        try:
            subprocess.run([os.environ.get("ADB", "adb"), "start-server"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _reconnect(self):
        """Runs adb-connect (on PATH, or this package's module) and returns
        (combined output, exit code)."""
        if self.connect_command:
            command = list(self.connect_command)
        elif shutil.which("adb-connect"):
            command = ["adb-connect"]
        else:
            command = [sys.executable, "-m", "adb_harness.connect"]
        if self.serial:
            command += ["--serial", self.serial]
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        except subprocess.TimeoutExpired:
            return "adb-connect timed out", 124
        except OSError as why:
            return "adb-connect is not runnable: %s" % why, 1
        return (result.stdout + result.stderr).strip(), result.returncode

    def run(self):
        """Runs until `stop()` or the attempt bound; returns a process exit code."""
        os.makedirs(self.out, exist_ok=True)
        with open(self.pidfile, "w") as handle:
            handle.write("%d\n" % os.getpid())
        self.running = True
        self.pid = os.getpid()
        self.attempts = 0
        self.devices_now = ()
        self._note_attempts()
        state = ""
        failure = ""
        failed_at = 0.0
        try:
            while not self._stop.is_set():
                self._adb_start()
                found = self._devices()
                self.devices_now = tuple(found)
                if found:
                    serial = found[0]
                    if state != serial:
                        state = serial
                        self._emit(WatchEvent("connected", self.devices_now,
                                              self.attempts, serial=serial))
                    failure = ""
                else:
                    if state:
                        state = ""
                        self._emit(WatchEvent("disconnected", (),
                                              self.attempts,
                                              detail="device list is empty"))
                    self.attempts += 1
                    self._note_attempts()
                    output, code = self._reconnect()
                    lines = [line for line in output.splitlines() if line.strip()]
                    if code == 0:
                        reconnected = ""
                        for line in lines:
                            if line.startswith("connected to "):
                                reconnected = line[len("connected to "):].strip()
                                break
                        self.devices_now = tuple(self._devices())
                        if not reconnected and self.devices_now:
                            reconnected = self.devices_now[0]
                        self._emit(WatchEvent("reconnected", self.devices_now,
                                              self.attempts, serial=reconnected))
                        failure = ""
                    else:
                        brief = " ".join(lines[-2:])
                        now = time.time()
                        if brief != failure or now - failed_at >= REPEAT_AFTER:
                            self.devices_now = tuple(self._devices())
                            self._emit(WatchEvent("error", self.devices_now,
                                                  self.attempts, detail=brief))
                            failure = brief
                            failed_at = now
                if self.max_attempts and self.attempts >= self.max_attempts:
                    self.devices_now = tuple(self._devices())
                    return 0 if self.devices_now else 1
                self._stop.wait(self.interval)
            return 0
        finally:
            self.running = False
            self.pid = None
            try:
                with open(self.pidfile) as handle:
                    ours = handle.read().strip() == str(os.getpid())
                if ours:
                    os.remove(self.pidfile)
            except OSError:
                pass


def append_log(path, message):
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a") as handle:
            handle.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), message))
    except OSError:
        pass


def status_command(args):
    status = read_status(args.out, args.serial)
    if args.json:
        emit(SCHEMA, running=status.running, pid=status.pid,
             devices=list(status.devices), attempts=status.attempts)
        return 0 if status.running else 1
    if status.running:
        print("adb-watch running (pid %d)" % status.pid)
        if status.devices:
            for serial in status.devices:
                print("device: %s" % serial)
        else:
            print("device: none")
        return 0
    print("adb-watch not running")
    return 1


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Keep the wireless-debugging adb connection alive.")
    parser.add_argument("--status", action="store_true",
                        help="is a watcher running, and is a device attached")
    parser.add_argument("--json", action="store_true",
                        help="one JSON object per state change, or for --status")
    parser.add_argument("--quiet", action="store_true",
                        help="leave the log alone; JSON objects still print")
    parser.add_argument("--interval", type=float,
                        default=os.environ.get("ADB_WATCH_INTERVAL", "15"),
                        help="seconds between checks (default 15)")
    parser.add_argument("--max-attempts", type=int, default=0,
                        help="stop after N reconnect attempts (0 is no bound)")
    parser.add_argument("--serial", default="",
                        help="watch this serial when more than one is attached")
    parser.add_argument("--version", action="version", version=VERSION)
    args = parser.parse_args(argv)
    out = output_dir()
    args.out = out

    if args.status:
        return status_command(args)

    try:
        watcher = Watcher(args.interval, args.max_attempts, out=out,
                          serial=args.serial)
    except ValueError as why:
        print("adb-watch: %s" % why, file=sys.stderr)
        return 2

    running = live_pid(os.path.join(out, "adb-watch.pid"))
    if running is not None:
        print("adb-watch is already running as pid %d" % running, file=sys.stderr)
        return 0

    log = log_path(out)

    def on_event(event):
        if not args.quiet:
            if event.event == "connected":
                append_log(log, "connected: %s" % event.serial)
            elif event.event == "reconnected":
                append_log(log, "reconnected: %s" % event.serial)
            elif event.event == "error":
                append_log(log, "not connected: %s" % event.detail)
        if args.json:
            fields = {"event": event.event, "devices": list(event.devices),
                      "attempts": event.attempts}
            if event.serial:
                fields["serial"] = event.serial
            if event.detail:
                fields["detail"] = event.detail
            emit(SCHEMA, **fields)

    watcher.on_event = on_event
    if not args.quiet:
        append_log(log, "watcher started (pid %d, every %gs)"
                   % (os.getpid(), watcher.interval))

    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, lambda _signum, _frame: watcher.stop())
    try:
        return watcher.run()
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == "__main__":
    sys.exit(main())
