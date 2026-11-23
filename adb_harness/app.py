"""The screenstream app as something on the device: its package, and its APK.

A capture prefers to subscribe to the app rather than drive a recorder, so a
machine that has never had it installed needs one thing from outside the
repository: the APK a release published. This module is that package's name, the
path of the status file it writes, and the one operation the capture needs
out of it - being installed.
"""

import hashlib
import os
import subprocess
import tempfile

from .device import adb, device_shell
from .errors import SourceUnavailable

PACKAGE = "io.github.mbanucu.screenstream.app"
ACTIVITY = PACKAGE + "/io.github.mbanucu.screenstream.ScreenStreamActivity"
STATUS = "/sdcard/Android/data/%s/files/screenstream.txt" % PACKAGE
# The repository whose releases carry the app, with a checksum beside the APK.
RELEASE_REPO = "MBanucu/screenstream"


def installed(serial=""):
    """Whether the app is on the device at all."""
    result = device_shell("pm path %s" % PACKAGE, serial)
    return result.returncode == 0 and "package:" in result.stdout


def apk(named=""):
    """The APK to install: the one named, or the newest release's, checksum checked.

    A machine recording this app for the first time is the one case where a run
    fetches a build artifact instead of building one, so it takes what the release
    published and refuses it when the checksum the release carries does not match.
    """
    named = named or os.environ.get("CAPTURE_APK", "")
    if named:
        if not os.path.isfile(named):
            raise SourceUnavailable("no APK at %s" % named)
        return named
    directory = tempfile.mkdtemp(prefix="screenstream-apk-")
    fetched = subprocess.run(
        ["gh", "release", "download", "--repo", RELEASE_REPO,
         "--pattern", "screenstream-*.apk*", "--dir", directory, "--clobber"],
        capture_output=True, text=True)
    if fetched.returncode != 0:
        raise SourceUnavailable(
            "the screenstream app is not installed and its release could not be "
            "fetched (%s); pass --apk or CAPTURE_APK"
            % (fetched.stderr.strip() or "gh failed"))
    files = sorted(name for name in os.listdir(directory) if name.endswith(".apk"))
    if not files:
        raise SourceUnavailable("the release carries no APK; pass --apk or CAPTURE_APK")
    path = os.path.join(directory, files[-1])
    checksum = path + ".sha256"
    if os.path.isfile(checksum):
        with open(checksum) as handle:
            expected = handle.read().split()[0]
        with open(path, "rb") as handle:
            actual = hashlib.sha256(handle.read()).hexdigest()
        if actual != expected:
            raise SourceUnavailable(
                "the downloaded APK does not match the release's checksum")
    return path


def ensure_installed(named="", serial=""):
    """Installs the app when it is missing, and does nothing when it is there."""
    if installed(serial):
        return None
    path = apk(named)
    result = adb(["install", "-r", path], serial)
    if result.returncode != 0 or "Success" not in result.stdout:
        raise SourceUnavailable(
            "could not install %s: %s" % (os.path.basename(path),
                                          (result.stderr or result.stdout).strip()))
    return path
