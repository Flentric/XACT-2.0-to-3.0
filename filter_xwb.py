#!/usr/bin/env python3
"""Remove short entries (DJ talk, ads, commercials) from XACT3 wave banks.

Works on banks already converted by xwb2to3.py (or any XACT3 .xwb). Entries
shorter than --min-seconds are dropped; with --top-rate, entries below the
bank's highest sample rate are dropped too. Everything else is kept as is:
version numbers (e.g. Dead Island's 65536), byte order, data packing, entry
names and the audio itself.

Removing entries shifts the positions of the ones after them.
"""

import argparse
import os
import struct
import sys

from xwb2to3 import (ENTRY_SIZE, FLAGS_COMPACT, FLAGS_SEEKTABLES, HEADER_SIZE, TAG_NAMES,
                     ConvertError, MiniFormat, Reader, align_up, expand_inputs, is_xact3)


def read_bank(data):
    """Parse an XACT3 bank into a dict with its entries."""
    if data[:4] == b"WBND":
        e = "<"
    elif data[:4] == b"DNBW":
        e = ">"
    else:
        raise ConvertError("not an XACT wave bank")
    r = Reader(data, e)
    version = r.u32(4)
    if not is_xact3(version):
        raise ConvertError(f"not an XACT3 bank (content version {version}); "
                           "convert it with xwb2to3.py first")
    segs = [(r.u32(12 + i * 8), r.u32(16 + i * 8)) for i in range(5)]
    bo = segs[0][0]
    flags, count = r.u32(bo), r.u32(bo + 4)
    if flags & FLAGS_COMPACT:
        raise ConvertError("compact banks are not supported")
    meta_elem, name_elem, alignment = r.u32(bo + 72), r.u32(bo + 76), r.u32(bo + 80)
    if meta_elem < ENTRY_SIZE:
        raise ConvertError(f"unexpected entry size {meta_elem}")
    wave = r.blob(*segs[4])
    names = r.blob(*segs[3])
    seek = r.blob(*segs[2])
    entries = []
    packed = False
    for i in range(count):
        eo = segs[1][0] + i * meta_elem
        meta = r.blob(eo, meta_elem)
        flags_dur, fmt_raw, off, length = struct.unpack_from(e + "4I", meta)
        if off + length > len(wave):
            raise ConvertError(f"entry {i}: play region outside wave data")
        if alignment and off % alignment:
            packed = True
        fmt = MiniFormat.unpack(fmt_raw, 46)
        name = b""
        if names and name_elem:
            name = names[i * name_elem:(i + 1) * name_elem].split(b"\0", 1)[0]
        seek_table = None
        if seek:
            rel = struct.unpack_from(e + "I", seek, 4 * i)[0]
            if rel != 0xFFFFFFFF:
                start = 4 * count + rel
                n = struct.unpack_from(e + "I", seek, start)[0]
                seek_table = seek[start:start + 4 * (n + 1)]
        entries.append(dict(index=i, meta=meta, fmt=fmt, duration=flags_dur >> 4,
                            audio=wave[off:off + length],
                            name=name.decode("latin-1"),
                            name_raw=names[i * name_elem:(i + 1) * name_elem] if name_elem else b"",
                            seek=seek_table))
    return dict(e=e, data=data, segs=segs, flags=flags, name_elem=name_elem,
                alignment=alignment, packed=packed, entries=entries)


def seconds(entry):
    return entry["duration"] / entry["fmt"].rate if entry["fmt"].rate else 0.0


def write_bank(bank, kept):
    """Rebuild the bank with only the `kept` entries, keeping its layout style."""
    e, data, segs = bank["e"], bank["data"], bank["segs"]
    bo = segs[0][0]
    alignment = max(bank["alignment"], 1)
    entry_align = 1 if bank["packed"] else alignment

    wave = bytearray()
    meta = bytearray()
    for ent in kept:
        off = align_up(len(wave), entry_align)
        wave += b"\0" * (off - len(wave)) + ent["audio"]
        m = bytearray(ent["meta"])
        struct.pack_into(e + "2I", m, 8, off, len(ent["audio"]))
        meta += m

    names = b"".join(ent["name_raw"] for ent in kept)
    seek = b""
    if any(ent["seek"] is not None for ent in kept):
        offsets, tables = [], b""
        for ent in kept:
            if ent["seek"] is None:
                offsets.append(0xFFFFFFFF)
            else:
                offsets.append(len(tables))
                tables += ent["seek"]
        seek = struct.pack(e + f"{len(kept)}I", *offsets) + tables

    bankdata = bytearray(data[bo:bo + 96])
    flags = bank["flags"] & ~FLAGS_SEEKTABLES | (FLAGS_SEEKTABLES if seek else 0)
    struct.pack_into(e + "2I", bankdata, 0, flags, len(kept))

    body = bytearray()
    out_segs = []
    pos = HEADER_SIZE
    wave_align = 4 if bank["packed"] else alignment
    for blob, seg_align in ((bankdata, 4), (meta, 4), (seek, 4), (names, 4),
                            (wave, wave_align)):
        start = align_up(pos, seg_align)
        body += b"\0" * (start - pos) + blob
        out_segs.append((start, len(blob)))
        pos = start + len(blob)
    header = data[:12] + b"".join(struct.pack(e + "2I", o, n) for o, n in out_segs)
    return bytes(header + body)


# rates within 2% count as the same (banks often mix e.g. 47999/48000/48001 Hz)
RATE_TOLERANCE = 0.02


def choose(entries, min_seconds, top_rate):
    """Return (kept, removed) with a reason for each removal."""
    max_rate = max((ent["fmt"].rate for ent in entries), default=0)
    kept, removed = [], []
    for ent in entries:
        if seconds(ent) < min_seconds:
            removed.append((ent, f"shorter than {min_seconds:g}s"))
        elif top_rate and ent["fmt"].rate < max_rate * (1 - RATE_TOLERANCE):
            removed.append((ent, f"lower rate than {max_rate} Hz"))
        else:
            kept.append(ent)
    return kept, removed


def fmt_time(sec):
    return f"{int(sec // 60)}:{int(sec % 60):02d}"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Remove short entries (DJ talk, ads) from XACT3 .xwb wave banks.")
    ap.add_argument("input", nargs="+", help=".xwb files and/or folders (searched recursively)")
    ap.add_argument("--min-seconds", type=float, default=90,
                    help="keep only entries at least this long (default 90)")
    ap.add_argument("--top-rate", action="store_true",
                    help="also drop entries with a clearly lower sample rate than the bank's "
                         "highest (more than 2%% lower)")
    ap.add_argument("--dry-run", action="store_true",
                    help="only list what would be kept/removed; write nothing")
    ap.add_argument("-o", "--output",
                    help="output directory (default: a 'songs_only' folder next to the input)")
    args = ap.parse_args(argv)

    failures = 0
    total_kept = total_removed = 0
    for path, out_dir in expand_inputs(args.input, out_name="songs_only"):
        try:
            with open(path, "rb") as f:
                bank = read_bank(f.read())
            kept, removed = choose(bank["entries"], args.min_seconds, args.top_rate)
            why = {id(ent): reason for ent, reason in removed}
            print(f"\n{path}")
            print(f"  {'#':>3}  {'length':>6}  {'Hz':>6}  {'codec':<5}  {'result':<28}  name")
            for ent in bank["entries"]:
                result = f"removed ({why[id(ent)]})" if id(ent) in why else "KEEP"
                print(f"  {ent['index']:>3}  {fmt_time(seconds(ent)):>6}  "
                      f"{ent['fmt'].rate:>6}  {TAG_NAMES.get(ent['fmt'].tag, '?'):<5}  "
                      f"{result:<28}  {ent['name']}")
            print(f"  -> keeping {len(kept)} of {len(bank['entries'])}")
            total_kept += len(kept)
            total_removed += len(removed)
            if args.dry_run:
                continue
            if not kept:
                raise ConvertError("every entry would be removed; bank left unchanged")
            out = os.path.join(args.output or out_dir, os.path.basename(path))
            if os.path.exists(out) and os.path.samefile(out, path):
                raise ConvertError(f"refusing to overwrite {out}")
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            with open(out, "wb") as f:
                f.write(write_bank(bank, kept))
            print(f"  saved {out}")
        except (ConvertError, OSError, struct.error) as exc:
            print(f"{path}: error: {exc}", file=sys.stderr)
            failures += 1
    print(f"\n{total_kept} entries kept, {total_removed} removed"
          + (" (preview only, nothing written)" if args.dry_run else ""))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
