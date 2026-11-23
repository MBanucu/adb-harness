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

## Use as a library

The same three commands are importable, and the library path does not print:
return values and the exceptions in `adb_harness.errors` are the contract. This
is for a test suite that wants to embed the recorder instead of forking it.

```python
from adb_harness import capture, connect

port = connect.find(timeout=8)          # Port | None
result = connect.connect(port)          # ConnectResult(serial, address, already)

with capture.Session(dir="out/capture", serial=result.serial) as run:
    run.event("check:diag")             # a file append: no process, no adb call
    frames = run.stop()                 # Recording(video, sidecar, events)
    frame = frames.frame_for("check:diag")
```

`connect.find` returns a `Port(address, port, serial, source)`, or `None` when
nothing is advertised; `source` is `mdns`, `port` (named with `port=...`) or
`attached`. `connect.connect` returns a `ConnectResult(serial, address, already)`
and does nothing to adb when the port is already attached. `find` and `connect`
raise `NoAdb` when adb is missing, `NoMdns` when python-zeroconf is, and
`NotConnected` when no address answers.

`capture.Session` owns start, event and stop. `event(name, data=None)` appends
one line, so a hundred moments cost a hundred appends and no adb calls.
`stop()` returns a `Recording`: `video` and `sidecar` are paths, `events` is the
mapped list, `data` is the sidecar dict, and `frame_for(name)` is the first
mapped event of that name (`frames_for` keeps every moment of a repeated name).
Stop raises `CaptureFailed` instead of exiting. ffmpeg is only needed by
`capture frames`, not by recording or mapping.

`watch.Watcher(interval=15, max_attempts=0, on_event=None)` is the reconnect loop
as an object: `.run()` blocks until `.stop()` or the attempt bound, `.status()`
returns `WatchStatus(running, pid, devices, attempts)`, and `on_event` receives a
`WatchEvent` for each state change. `adb_harness/watch.sh` is only a caller of
this module now.

A serial is what makes two attached devices unambiguous. It can be passed to
`connect.find`, `capture.Session`/`Session.stop`, and every command:
`adb-connect --serial S`, `adb-watch --serial S`, and
`capture start --serial S` (also accepted after any capture verb). The commands
still keep their documented output and exit codes; only the library skips the
printing.

## adb-connect

```sh
adb-connect               # discover, connect, say which port
adb-connect --pair        # same lookup, for the pairing port
adb-connect --port 38109  # skip discovery
adb-connect --json        # one JSON object on stdout, for a harness
adb-connect --quiet       # no progress lines; the final output still prints
adb-connect --serial S    # prefer this device when more than one is attached
adb-connect --version     # print the version and exit
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
adb-watch --status --json        # the same, as one JSON object
adb-watch --json                 # one JSON object per state change
adb-watch --interval 5 --max-attempts 10
adb-watch --serial S             # watch one device when several are attached
kill "$(cat out/adb-watch.pid)"  # stop a running watcher
```

While `adb devices` shows nothing, it runs `adb-connect` and logs the result. A
repeated failure is logged again only when the message changes or ten minutes have
passed, so a phone left with Wireless debugging off does not fill the log.
`ADB_WATCH_INTERVAL` (default 15 s), `ADB_WATCH_OUT` (pid and log directory,
default `out/` in the working directory) and `ADB_WATCH_LOG` change that.
`--interval` overrides the interval; `--max-attempts N` stops after `N` reconnect
attempts (`0`, the default, means no bound) and exits `0` if a device is attached
and `1` if not. `--quiet` leaves the log alone but still writes the JSON objects.

## capture

```sh
capture start --dir out/capture     # one recording for the run
capture start --serial S            # the device to record, when two are attached
capture start --json                # {"recording":true,"source":"...","dir":"..."}
capture event session:typed         # mark a moment; no adb call
capture stop                        # stop, and map events to frames
capture show                        # every event of a recording, with its frame
capture show --json                 # the sidecar as one object, with a schema
capture frames --event session:typed --out out/frames
capture frames --at 12.5,40 --out out/frames
capture --version                   # print the version and exit
```

Every capture subcommand accepts `--serial` (before or after the verb). `start`
records it and `stop` uses it, so two attached devices are not ambiguous;
`event`, `show` and `frames` accept it for a caller that writes one command line
shape.

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

A machine that has never had the app installed does not need a build step: `start`
checks `pm path`, and when the app is missing it takes the APK from the latest
GitHub release (`gh release download`), checks it against the checksum the release
carries, and installs it — 4 s from nothing to a recording, measured here. `--apk
PATH` or `CAPTURE_APK` uses a build from a checkout instead, and a download that
does not match its checksum is refused rather than installed.

`frames` needs `ffmpeg` on `PATH`; `stop` says what the offset between the two
clocks was, and where the device is older than that metadata track (Android 12
and older) screenrecord itself has no per-frame times to offer.

## Machine-readable output

Every JSON object names its shape and the version of that shape, so a harness can
refuse a tool it does not know:

```json
{"schema":"adb-connect","version":1,"ok":true,"address":"127.0.0.1:38109","port":38109,"serial":"127.0.0.1:38109","source":"mdns","waited_s":1.2}
```

`adb-connect --json` prints one object, success or failure, on stdout. On success
`source` is `mdns`, `port` (a named `--port`) or `attached` (a device was already
there). A failure keeps the same keys `ok` and `port` (`null` when no port is
known), and adds `reason` and `detail`; `reason` is one of `no-service`,
`stale-port`, `no-adb` and `no-mdns`:

```json
{"schema":"adb-connect","version":1,"ok":false,"reason":"stale-port","detail":"127.0.0.1:38109: failed to connect: Connection refused","port":38109}
```

`adb-watch --status --json` is one object; without `--status`, `--json` writes one
object per state change to stdout:

```json
{"schema":"adb-watch","version":1,"running":true,"pid":4242,"devices":["127.0.0.1:38109"],"attempts":0}
{"schema":"adb-watch","version":1,"event":"reconnected","devices":["127.0.0.1:38109"],"attempts":2,"serial":"127.0.0.1:38109"}
```

`event` is `connected`, `reconnected`, `disconnected` or `error`; an `error` also
carries the `detail` adb-connect last said.

`capture start --json` names the recording that started:

```json
{"schema":"capture-start","version":1,"recording":true,"source":"stream","dir":"out/capture","serial":"127.0.0.1:38109"}
```

`capture show --json` is the sidecar `stop` wrote, with the schema added rather
than a second format. Written back to a file it is a `--sidecar` for `frames`:

```sh
capture show --json > out/capture/shown.json
capture frames --sidecar out/capture/shown.json --event session:typed --out out/frames
```

```json
{"schema":"capture-recording","version":1,"recording":"recording.mp4","source":"screenrecord","size":1843200,"duration":12.34,"events":[{"name":"session:typed","host":51.02,"timed":"host","frame":8,"since_frame_ms":21.0,"video_s":0.264}],"frames":[51000000000,51033000000],"sync":{"clock":"elapsed","unit_ns":1000000000,"frame_count":2,"epoch_offset_ns":0,"offset":-49.9,"offset_samples":2,"offset_spread_ms":2.1,"round_trip_ms":18.4}}
```

`--quiet` suppresses progress lines for `adb-connect`, `start` and the watcher's
log, but never the final object. `--version` on `adb-connect`, `adb-watch` and
`capture` prints the command and its version and exits `0`.

### Exit codes

One table, used by all three commands:

| code | meaning |
|---|---|
| `0` | success |
| `1` | failed to connect or record: nothing advertised, a port that nothing answers, no device, screenrecord would not start, frames could not be written |
| `2` | usage error: an unknown flag, a missing verb or argument, `--at` that is not seconds, nothing asked for with `frames` |
| `3` | a prerequisite is missing: no `adb`, no `python-zeroconf`, or no `ffmpeg` for `frames` |

The JSON objects carry the matching `reason`; the exit code is what a shell
should branch on.

## From a checkout

Each module is also runnable, so a checkout needs no install:

```sh
python3 -m adb_harness.capture show --dir out/capture
python3 -m adb_harness.connect --timeout 4
python3 -m adb_harness.watch --status
```

and `bash adb_harness/watch.sh` runs the same Python watcher from a checkout. The
script is only a caller now: it finds the package and runs
`python3 -m adb_harness.watch` with the arguments it was given.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Everything under `tests/` runs without a phone, adb on `PATH` or a network.
`tests/fakeadb.py` writes an `adb` (and `adb-connect`) executable into the test's
`TemporaryDirectory` and puts it first on `PATH`; a JSON scenario says what
`devices`, `connect`, `get-state` and a device shell answer, and a successful
connect updates the device table the next call sees. Each test file covers one
module:

| file | module it covers |
|---|---|
| `tests/test_capture.py` | `capture.py`'s event-to-frame mapping, over a synthetic MP4 of the shape `screenrecord` writes: an `ftyp`, a `moov` with a metadata track whose one sample is a Winscope payload, and that sample in `mdat`. Checks the frame that was on screen, how long before the event it appeared, the container time it gets, events the device timed itself, events before the first and after the last frame, a container and metadata track that disagree, both magic versions, the elapsed clock winning over the monotonic one, no metadata track, frame times out of order, and a truncated file. |
| `tests/test_connect_deviceless.py` | `connect.py`'s `attached`, `adb`, `discover`, `find` and `connect`. The fake adb covers a device table with offline and unauthorised entries, an already-attached device, a named serial, and the `stale-port` and `no-service` JSON reasons. Real `zeroconf.ServiceInfo` objects fed through `discover`'s callback cover the connect service winning over the pairing one, a phone advertising its own loopback address, no service within the timeout, and two advertised phones where the one that answers wins. Those cases skip with the reason when the optional `zeroconf` extra is not installed; the fake-adb cases do not need it. |
| `tests/test_watch_deviceless.py` | `watch.py`'s `Watcher` and `--status`. Three ticks at a millisecond interval cover one `adb-connect` per tick with no device, a repeated failure whose log line waits ten minutes, a device that appears causing an attached exit, the JSON state-change stream, and status with a live pidfile, no pidfile and a stale one. |
| `tests/test_capture_lifecycle.py` | `capture.py`'s `Session` order: `stop` with no `start`, two `start`s on one session, an event before `start` (raised by the library, a no-op from the command), `show` with no recording, and a sidecar whose video is missing, each with the documented error or exit code. |
| `tests/test_library.py` | the library surface of `connect`, `capture.Session` and `watch.Watcher`: return values, exceptions, an event that costs no process, and a watcher asked about in process. |
| `tests/test_cli.py` | the commands' machine-readable output, exit codes and unchanged prose, with adb and mDNS replaced by Mocks. |

What needs a real device — recording, pulling, extracting frames, reconnecting
after a reboot — is checked where the tools are used, in
`android-accessible-terminal`'s e2e suite. CI also builds the package on every
push and installs the wheel and the sdist in a fresh virtualenv, because tests
against the checkout cannot see a wheel with the wrong entry points or a missing
`watch.sh`.

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
