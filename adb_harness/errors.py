"""A recording strategy could not run; `auto` falls back to screenrecord."""


class SourceUnavailable(Exception):
    """A way of recording cannot run here, and `auto` should try screenrecord."""
