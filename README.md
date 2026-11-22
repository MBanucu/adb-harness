# adb-harness

Three tools for driving one phone over `adb` from a shell: find it when its
wireless-debugging port changes, keep it connected, and record the screen so a
moment in a run can be looked at afterwards.

They are what `android-accessible-terminal` uses to test an accessibility service
on a real device, pulled out into their own repository so another project can use
them without taking that suite along.

| tool | what it does | needs |
|---|---|---|
| `adb-connect.py` | finds the wireless-debugging port over mDNS and runs `adb connect` | `adb`, `zeroconf` |
| `adb-watch.sh` | loop that reconnects whenever the device list goes empty | `adb`, `adb-connect.py` |
| `capture.py` | one `screenrecord` for a run, an event beside each moment, and the exact frame for an event | `adb`, `ffmpeg` |

## adb-connect.py

```sh
./adb-connect.py               # discover, connect, say which port
./adb-connect.py --pair        # same lookup, for the pairing port
./adb-connect.py --port 38109  # skip discovery
```

The port changes every time Wireless debugging is turned off and on again, so a
session that worked yesterday finds nothing today. Termux's adb cannot answer
`adb mdns services` ("mdns is not supported by this version of adb") and `ss` and
`/proc/net/tcp6` are closed to an app, which is why the lookup goes through
[python-zeroconf](https://pypi.org/project/zeroconf/) (`pip install zeroconf`).

A phone that has never been paired with this adb key has to be paired once, by
hand, with the code it shows: `./adb-connect.py --pair` prints the command.

## adb-watch.sh

```sh
bash adb-watch.sh                 # run in the foreground
bash adb-watch.sh --status        # is it running, and is a device attached
kill "$(cat out/adb-watch.pid)"   # stop a running watcher
```

While `adb devices` shows nothing, it runs `adb-connect.py` and logs the result.
A repeated failure is logged again only when the message changes or ten minutes
have passed, so a phone left with Wireless debugging off does not fill the log.
`ADB_WATCH_INTERVAL` (default 15 s), `ADB_WATCH_OUT` (pid and log directory,
default `out/` beside the script) and `ADB_WATCH_LOG` change that.

## capture.py

```sh
python3 capture.py start --dir out/capture     # one screenrecord for the run
python3 capture.py event session:typed         # mark a moment; no adb call
python3 capture.py stop                        # stop, pull, and map events to frames
python3 capture.py show                        # every event of a recording, with its frame
python3 capture.py frames --event session:typed --out out/frames
python3 capture.py frames --at 12.5,40 --out out/frames
```

`stop` keeps the video, the events and a sidecar in `--dir`; `frames` takes a
single frame out of the recording when somebody asks, which is what a screenshot
is for once the run is over. The mapping is exact rather than "the video starts
when I pressed start": `screenrecord` writes the presentation time of every frame
in the device's elapsed-realtime clock into a metadata track beside the video,
which is the same clock `/proc/uptime` reports, so a moment logged on this side
becomes the frame that was on screen then. `frames` needs `ffmpeg` on `PATH`;
`stop` says what the offset between the two clocks was, and where the device is
older than that metadata track (Android 12 and older) it falls back to the
recording's own start and says so.

## Using these from another repository

Each release is a tag with the files at its root, so a project can pin a version
and check what it fetched:

```sh
version=0.1.0
base=https://raw.githubusercontent.com/MBanucu/adb-harness/v$version
curl -fsSL "$base/adb-connect.py" -o adb-connect.py
curl -fsSL "$base/adb-watch.sh" -o adb-watch.sh
curl -fsSL "$base/capture.py" -o capture.py
```

Nothing here is imported as a library: run the files with `python3` or `bash`.

## License

GPL-3.0-only. See [LICENSE](LICENSE).
