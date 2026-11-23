# adb-harness

Three commands for driving one phone over `adb` from a shell: find it when its
wireless-debugging port changes, keep it connected, and record the screen so a
moment in a run can be looked at afterwards.

They are what `android-accessible-terminal` uses to test an accessibility service
on a real device, published on their own so another project can use them without
taking that suite along.

```sh
pip install adb-harness
pip install "adb-harness[mdns]"   # and python-zeroconf, which adb-connect needs
```

| command | what it does | needs |
|---|---|---|
| `adb-connect` | finds the wireless-debugging port over mDNS and runs `adb connect` | `adb`, `zeroconf` |
| `adb-watch` | loop that reconnects whenever the device list goes empty | `adb`, `adb-connect` |
| `capture` | one recording for a run, an event beside each moment, and the exact frame for an event | `adb`, `ffmpeg`, screenstream (optional) |

## adb-connect

```sh
adb-connect               # discover, connect, say which port
adb-connect --pair        # same lookup, for the pairing port
adb-connect --port 38109  # skip discovery
```

The port changes every time Wireless debugging is turned off and on again, so a
session that worked yesterday finds nothing today. Termux's adb cannot answer
`adb mdns services` ("mdns is not supported by this version of adb") and `ss` and
`/proc/net/tcp6` are closed to an app, which is why the lookup goes through
[python-zeroconf](https://pypi.org/project/zeroconf/).

A phone that has never been paired with this adb key has to be paired once, by
hand, with the code it shows: `adb-connect --pair` prints the command.

## adb-watch

```sh
adb-watch                        # run in the foreground
adb-watch --status               # is it running, and is a device attached
kill "$(cat out/adb-watch.pid)"  # stop a running watcher
```

While `adb devices` shows nothing, it runs `adb-connect` and logs the result. A
repeated failure is logged again only when the message changes or ten minutes have
passed, so a phone left with Wireless debugging off does not fill the log.
`ADB_WATCH_INTERVAL` (default 15 s), `ADB_WATCH_OUT` (pid and log directory,
default `out/` in the working directory) and `ADB_WATCH_LOG` change that.

## capture

```sh
capture start --dir out/capture     # one recording for the run
capture event session:typed         # mark a moment; no adb call
capture stop                        # stop, and map events to frames
capture show                        # every event of a recording, with its frame
capture frames --event session:typed --out out/frames
capture frames --at 12.5,40 --out out/frames
```

`stop` keeps the video, the events and a sidecar in `--dir`; `frames` takes a
single frame out of the recording when somebody asks, which is what a screenshot
is for once the run is over. The mapping is exact rather than "the video starts
when I pressed start": every frame carries its presentation time in the device's
elapsed-realtime clock, which is the same clock `/proc/uptime` reports, so a
moment logged on this side becomes the frame that was on screen then.

Two ways to record, one mapping. `start` prefers **screenstream**: it brings the
app up, subscribes to its H.264 stream, and `stop` saves what arrived as a
fragmented MP4 — one hardware encode, no second process competing for the
display, and the device time in the file's own init segment. When that cannot
run (app not installed, projection not granted, port silent) it falls back to the
device's **screenrecord** and says why; `--source stream` or `--source
screenrecord` pins one, and `--source auto` is the default. `screenrecord` writes
its frame times into a metadata track beside the video instead, so the sidecar's
`sync` block differs in one field (`base_ns` for the stream, the track's own
`epoch_offset_ns` for screenrecord) while the events come out the same.

`frames` needs `ffmpeg` on `PATH`; `stop` says what the offset between the two
clocks was, and where the device is older than that metadata track (Android 12
and older) screenrecord itself has no per-frame times to offer.

## From a checkout

Each module is also runnable, so a checkout needs no install:

```sh
python3 -m adb_harness.capture show --dir out/capture
python3 -m adb_harness.connect --timeout 4
python3 -m adb_harness.watch --status
```

and `bash adb_harness/watch.sh` runs the watcher without Python finding the
package, falling back to `python3 -m adb_harness.connect` for reconnects.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

The mapping is arithmetic over a recording, so it has a test suite that needs no
phone: `tests/test_capture.py` builds a synthetic MP4 of the shape `screenrecord`
writes — an `ftyp`, a `moov` with a metadata track whose one sample is a Winscope
payload, and that sample in `mdat` — and checks what an event becomes against it:
the frame that was on screen, how long before the event it appeared, the container
time it gets, events the device timed itself (which take no offset), events before
the first frame and after the last, and a container whose frame list disagrees
with the metadata track. The parser is covered the same way: both magic versions,
the elapsed clock winning over the monotonic one, no metadata track at all, frame
times out of order, and a truncated file.

What needs a device — recording, pulling, extracting frames, reconnecting — is
checked where the tools are used, in `android-accessible-terminal`'s e2e suite.
CI also builds the package on every push and installs the wheel and the sdist in a
fresh virtualenv, because tests against the checkout cannot see a wheel with the
wrong entry points or a missing `watch.sh`.

## Releasing

```sh
# bump __version__ in adb_harness/__init__.py, commit, then
git tag v0.2.0 && git push origin v0.2.0
```

The tag runs `.github/workflows/release.yml`, which refuses a tag that disagrees
with the package, builds, installs the wheel in a directory with no checkout to
fall back on, and uploads to PyPI with
[trusted publishing](https://docs.pypi.org/trusted-publishers/) — no token is
stored here. PyPI trusts `MBanucu/adb-harness`, workflow `release.yml`,
environment `pypi`; that entry started as a pending publisher, which is what
created the project.

## License

GPL-3.0-only. See [LICENSE](LICENSE).
