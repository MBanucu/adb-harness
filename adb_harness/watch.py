"""Runs the watcher script that ships with this package.

The watcher is a shell loop, not a Python program, so this is the entry point that
lets it be installed as a command: `adb-watch` finds `watch.sh` in the package and
runs it with the same arguments. `python3 -m adb_harness.watch` does the same from
a checkout.
"""

import subprocess
import sys
from importlib import resources


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    script = resources.files("adb_harness").joinpath("watch.sh")
    with resources.as_file(script) as path:
        return subprocess.call(["bash", str(path), *argv])


if __name__ == "__main__":
    sys.exit(main())
