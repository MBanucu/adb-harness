"""Reading an MP4 by hand: the container's times and the metadata track's."""
import struct
import subprocess
import sys

MAGIC_V1 = b"#VV1NSC0PET1ME!#"
MAGIC_V2 = b"#VV1NSC0PET1ME2#"
CONTAINERS = {b"moov", b"trak", b"mdia", b"minf", b"stbl", b"dinf"}


# --- the MP4's metadata track ------------------------------------------------


def boxes(data, start, end):
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack(">I4s", data[pos:pos + 8])
        header = 8
        if size == 1:
            size = struct.unpack(">Q", data[pos + 8:pos + 16])[0]
            header = 16
        if size < header or pos + size > end:
            return
        yield kind, pos, header, size
        if kind in CONTAINERS:
            yield from boxes(data, pos + header, pos + size)
        pos += size


def full_box(body, fields):
    """Read a version-0 full box's fields: a 4-byte version/flags, then values."""
    values = struct.unpack(">" + fields, body[4:4 + struct.calcsize(">" + fields)])
    return values


def track_tables(data, start, end):
    """What a track's sample tables say: handler, sizes, offsets, chunk layout."""
    handler = None
    sizes, offsets, chunks = [], [], []
    for kind, pos, header, size in boxes(data, start, end):
        body = data[pos + header:pos + size]
        if kind == b"hdlr":
            handler = body[8:12]
        elif kind == b"stsz":
            default, count = full_box(body, "II")
            sizes = [default] * count if default else list(
                struct.unpack(">%dI" % count, body[12:12 + 4 * count]))
        elif kind == b"stco":
            count = full_box(body, "I")[0]
            offsets = list(struct.unpack(">%dI" % count, body[8:8 + 4 * count]))
        elif kind == b"co64":
            count = full_box(body, "I")[0]
            offsets = list(struct.unpack(">%dQ" % count, body[8:8 + 8 * count]))
        elif kind == b"stsc":
            count = full_box(body, "I")[0]
            for i in range(count):
                chunks.append(struct.unpack(">III", body[8 + 12 * i:20 + 12 * i])[:2])
    return handler, sizes, offsets, chunks


def track_samples(data, offsets, chunks, sizes):
    index, out = 0, []
    for piece, (first_chunk, per_chunk) in enumerate(chunks):
        last = (chunks[piece + 1][0] - 1) if piece + 1 < len(chunks) else len(offsets)
        for chunk in range(first_chunk, last + 1):
            offset = offsets[chunk - 1]
            for _ in range(per_chunk):
                if index >= len(sizes):
                    return out
                out.append(data[offset:offset + sizes[index]])
                offset += sizes[index]
                index += 1
    return out


def parse_metadata(path):
    """Frame times from the Winscope metadata track: {clock, frames, epoch_offset}."""
    with open(path, "rb") as handle:
        data = handle.read()
    best = None
    for kind, pos, header, size in boxes(data, 0, len(data)):
        if kind != b"trak":
            continue
        handler, sizes, offsets, chunks = track_tables(data, pos + header, pos + size)
        if handler != b"meta":
            continue
        for payload in track_samples(data, offsets, chunks, sizes):
            # Version 2 first: the elapsed clock, which /proc/uptime also reports.
            if payload.startswith(MAGIC_V2):
                epoch_offset = struct.unpack("<q", payload[20:28])[0]
                count = struct.unpack("<I", payload[28:32])[0]
                stamps = struct.unpack("<%dQ" % count, payload[32:32 + 8 * count])
                found = {"clock": "elapsed", "unit": 1e9, "frames": list(stamps),
                         "epoch_offset_ns": epoch_offset}
            elif payload.startswith(MAGIC_V1):
                count = struct.unpack("<I", payload[16:20])[0]
                stamps = struct.unpack("<%dQ" % count, payload[20:20 + 8 * count])
                found = {"clock": "monotonic", "unit": 1e6, "frames": list(stamps)}
            else:
                continue
            if found["frames"] and (best is None or found["clock"] == "elapsed"):
                best = found
    if best is None:
        return None
    frames = best["frames"]
    if any(b < a for a, b in zip(frames, frames[1:])):
        sys.exit("capture: the metadata track's frame times are not in order")
    return best


def container_pts(path):
    """The container's own pts per frame, in presentation order, or nothing.

    No ffprobe on this machine, or a file it cannot read, is a recording without
    container times rather than a failure: the frame index and the metadata
    track's own times still say which picture was on screen.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "frame=pts_time", "-of", "csv=p=0", path],
            capture_output=True, text=True)
    except OSError:
        return []
    if result.returncode != 0:
        return []
    stamps = []
    for line in result.stdout.split():
        try:
            stamps.append(float(line))
        except ValueError:
            pass
    return stamps


# --- commands ----------------------------------------------------------------
