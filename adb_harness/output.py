"""The JSON contract and the exit codes, shared by the three commands.

Every machine-readable object names the thing it is (`schema`) and the version
of that shape (`version`), so a harness can refuse a tool whose output it does
not know instead of misreading it. The version is the shape's, not this
package's: adding a field is a new version even when the package's own version
is unchanged.

The exit codes are the same for every command, whether it printed prose or
JSON:

    0  success
    1  failed to connect or record (including "nothing advertised" and a port
       that nothing answers on)
    2  usage error (an unknown flag, no verb, a missing argument)
    3  a prerequisite is missing (adb, python-zeroconf, or ffmpeg for frames)
"""

import json

SCHEMA_VERSION = 1

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_PREREQUISITE = 3


def emit(schema, **fields):
    """One object on stdout: the schema, its version, then the command's fields."""
    print(json.dumps({"schema": schema, "version": SCHEMA_VERSION, **fields},
                     separators=(",", ":")))
