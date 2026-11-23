#!/usr/bin/env bash
# A caller of the Python watcher, kept so the script itself can be run.
#
# The loop, the pidfile, the attempt count and the state changes live in
# `adb_harness/watch.py`; this only finds the package (installed, or the
# checkout this file sits in) and runs it. `adb-watch` is the installed entry
# point and does the same without a shell.
#
#   adb-watch                        # run in the foreground
#   adb-watch --status               # is it running, and is a device attached
#   adb-watch --status --json        # the same, as one JSON object
#   adb-watch --json                 # one JSON object per state change
#   adb-watch --interval 5 --max-attempts 10
#   kill "$(cat out/adb-watch.pid)"  # stop a running watcher
#
# Environment: ADB_WATCH_INTERVAL, ADB_WATCH_OUT and ADB_WATCH_LOG are read by
# the Python module. ADB_HARNESS_PYTHON names the interpreter to use.
set -u

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# Installed, `here` is site-packages/adb_harness; from a checkout, it is
# <checkout>/adb_harness. In both cases the parent is the import root.
export PYTHONPATH="$here/..${PYTHONPATH:+:$PYTHONPATH}"

exec "${ADB_HARNESS_PYTHON:-python3}" -m adb_harness.watch "$@"
