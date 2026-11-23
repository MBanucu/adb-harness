"""A fake `adb` and `adb-connect` on PATH, driven by a JSON scenario file.

The tests here use the real `connect`, `watch` and `capture` code: what is
replaced is the process at the other end. A scenario says what `adb devices`,
`adb connect SERIAL`, `adb get-state` and a device shell answer; the scripts
update a small state file so a successful connect is visible to the next
`adb devices`, exactly as it would be after a real one.

    fake = FakeAdb(directory, {
        "devices": [["127.0.0.1:38109", "device"]],
        "connect_default": {"output": "failed", "code": 1},
    })
    with mock.patch.dict(os.environ, fake.environment()):
        ...

`calls()` reads back every invocation, which is how "one connect per tick" and
"nothing connects an attached device" are asserted. Nothing here touches the
network or a device, and every file it writes is under the TemporaryDirectory the
test passed in.
"""

import json
import os
import sys

ADB_SCRIPT = r'''#!{python}
import json
import os
import sys


def load(path, fallback):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return fallback


scenario = load(os.environ["FAKE_ADB_SCENARIO"], {{}})
state_path = os.environ["FAKE_ADB_STATE"]
state = load(state_path, None)
if state is None:
    state = {{"devices": [list(entry) for entry in scenario.get("devices", [])]}}
    with open(state_path, "w") as handle:
        json.dump(state, handle)

argv = sys.argv[1:]
calls = os.environ.get("FAKE_ADB_CALLS")
if calls:
    with open(calls, "a") as handle:
        handle.write(json.dumps(["adb", *argv]) + "\n")


def save():
    with open(state_path, "w") as handle:
        json.dump(state, handle)


def apply(entry):
    """A connect, start-server or shell changes the device table it names."""
    removed = entry.get("remove", [])
    if removed:
        state["devices"] = [pair for pair in state["devices"]
                            if pair[0] not in removed]
    for serial in entry.get("add", []):
        if not any(pair[0] == serial for pair in state["devices"]):
            state["devices"].append([serial, entry.get("state", "device")])
    save()


def say(entry):
    text = entry.get("output", "")
    if text:
        sys.stdout.write(text)
        if not text.endswith("\n"):
            sys.stdout.write("\n")
    error = entry.get("stderr", "")
    if error:
        sys.stderr.write(error)
        if not error.endswith("\n"):
            sys.stderr.write("\n")
    return entry.get("code", 0)


serial = ""
if argv[:1] == ["-s"] and len(argv) > 1:
    serial = argv[1]
    argv = argv[2:]
command = argv[0] if argv else ""

if command == "devices":
    print("List of devices attached")
    for device_serial, device_state in state["devices"]:
        print("%s\t%s" % (device_serial, device_state))
    sys.exit(0)
if command == "connect":
    address = argv[1] if len(argv) > 1 else ""
    entry = (scenario.get("connect", {{}}).get(address)
             or scenario.get("connect_default") or {{}})
    apply(entry)
    sys.exit(say(entry))
if command == "get-state":
    entry = scenario.get("get_state", {{"output": "device", "code": 0}})
    sys.exit(say(entry))
if command == "start-server":
    sys.exit(say(scenario.get("start_server", {{"code": 0}})))
if command == "shell":
    entry = scenario.get("shell", {{"output": "1000.0 2000.0\n", "code": 0}})
    apply(entry)
    sys.exit(say(entry))
sys.exit(say(scenario.get("other", {{"code": 0}})))
'''

ADB_CONNECT_SCRIPT = r'''#!{python}
import json
import os
import sys


def load(path, fallback):
    try:
        with open(path) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return fallback


scenario = load(os.environ["FAKE_ADB_SCENARIO"], {{}})
state_path = os.environ["FAKE_ADB_STATE"]
state = load(state_path, {{"devices": []}})

argv = sys.argv[1:]
calls = os.environ.get("FAKE_ADB_CALLS")
if calls:
    with open(calls, "a") as handle:
        handle.write(json.dumps(["adb-connect", *argv]) + "\n")

entry = scenario.get("adb_connect", {{"output": "", "code": 0}})
for serial in entry.get("add", []):
    if not any(pair[0] == serial for pair in state["devices"]):
        state["devices"].append([serial, entry.get("state", "device")])
for serial in entry.get("remove", []):
    state["devices"] = [pair for pair in state["devices"] if pair[0] != serial]
with open(state_path, "w") as handle:
    json.dump(state, handle)

text = entry.get("output", "")
if text:
    sys.stdout.write(text)
    if not text.endswith("\n"):
        sys.stdout.write("\n")
error = entry.get("stderr", "")
if error:
    sys.stderr.write(error)
    if not error.endswith("\n"):
        sys.stderr.write("\n")
sys.exit(entry.get("code", 0))
'''


class FakeAdb:
    """`adb` and `adb-connect` executables that answer from a scenario dict."""

    def __init__(self, directory, scenario=None):
        self.directory = directory
        self.bin = os.path.join(directory, "bin")
        os.makedirs(self.bin, exist_ok=True)
        self.scenario_path = os.path.join(directory, "adb-scenario.json")
        self.state_path = os.path.join(directory, "adb-state.json")
        self.calls_path = os.path.join(directory, "adb-calls.jsonl")
        self.set_scenario(scenario or {})
        self._write("adb", ADB_SCRIPT)
        self._write("adb-connect", ADB_CONNECT_SCRIPT)

    def _write(self, name, template):
        path = os.path.join(self.bin, name)
        with open(path, "w") as handle:
            handle.write(template.format(python=sys.executable))
        os.chmod(path, 0o755)
        return path

    def set_scenario(self, scenario):
        """Replace the answers; the device state keeps what it already had."""
        with open(self.scenario_path, "w") as handle:
            json.dump(scenario, handle, indent=2)

    def reset(self, scenario=None):
        """Forget the calls and the connected devices, then apply a scenario."""
        if os.path.exists(self.calls_path):
            os.remove(self.calls_path)
        if os.path.exists(self.state_path):
            os.remove(self.state_path)
        if scenario is not None:
            self.set_scenario(scenario)

    def environment(self):
        """The environment a subprocess needs to find and drive the fakes."""
        env = dict(os.environ)
        env["PATH"] = self.bin + os.pathsep + env.get("PATH", "")
        env["FAKE_ADB_SCENARIO"] = self.scenario_path
        env["FAKE_ADB_STATE"] = self.state_path
        env["FAKE_ADB_CALLS"] = self.calls_path
        return env

    def calls(self, program=None):
        """Every recorded invocation, optionally only one program's."""
        try:
            with open(self.calls_path) as handle:
                recorded = [json.loads(line) for line in handle if line.strip()]
        except OSError:
            return []
        if program is None:
            return recorded
        return [call for call in recorded
                if call and call[0] == program]

    def devices(self):
        """The device table the fake currently reports."""
        try:
            with open(self.state_path) as handle:
                return [tuple(pair) for pair in json.load(handle)["devices"]]
        except (OSError, ValueError, KeyError):
            return []
