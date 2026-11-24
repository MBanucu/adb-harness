"""The emulator smoke script's preflight, without an emulator.

The full sequence runs only in `.github/workflows/emulator-e2e.yml`; these tests
cover the part that must never become a false green: `tests/emulator.sh` exits
non-zero with a clear message when adb has no attached emulator. A fake adb on
PATH supplies the device table, so nothing here boots or pretends to boot an AVD.
"""

import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fakeadb import FakeAdb  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tests", "emulator.sh")


def run_script(*args, env=None):
    return subprocess.run(["bash", SCRIPT, *args], cwd=ROOT, env=env,
                          capture_output=True, text=True)


class EmulatorScriptPreflightTest(unittest.TestCase):
    def test_syntax(self):
        result = subprocess.run(["bash", "-n", SCRIPT],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_refuses_to_pass_without_an_attached_emulator(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeAdb(directory, {"devices": []})
            result = run_script("--check-device", env=fake.environment())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no attached emulator", result.stderr)
        self.assertNotIn("emulator smoke: attached", result.stdout)

    def test_accepts_an_attached_emulator(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeAdb(directory, {
                "devices": [["emulator-5554", "device"]],
            })
            result = run_script("--check-device", env=fake.environment())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("emulator smoke: attached emulator-5554", result.stdout)


if __name__ == "__main__":
    unittest.main()
