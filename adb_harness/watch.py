"""Runs the watcher script that ships with this package.

The watcher is a shell loop, not a Python program, so this is the entry point that
lets it be installed as a command: `adb-watch` finds `watch.sh` in the package and
runs it with the same arguments. `python3 -m adb_harness.watch` does the same from
a checkout.

The script reconnects by running `adb-connect`, and a console script is only on
`PATH` when the environment it was installed into is activated. `pipx` and a
virtualenv called by absolute path are both used without that, so the interpreter's
own `bin/` goes on `PATH` and its path is passed along as ADB_HARNESS_PYTHON for
the script's fallback.
"""

import os
import subprocess
import sys
from importlib import resources

from . import __version__

VERSION = "adb-watch %s" % __version__


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--version" in argv:
        print(VERSION)
        return 0
    script = resources.files("adb_harness").joinpath("watch.sh")

    env = dict(os.environ)
    bindir = os.path.dirname(sys.executable)
    if bindir:
        env["PATH"] = bindir + os.pathsep + env.get("PATH", "")
    env["ADB_HARNESS_PYTHON"] = sys.executable

    with resources.as_file(script) as path:
        return subprocess.call(["bash", str(path), *argv], env=env)


if __name__ == "__main__":
    sys.exit(main())
