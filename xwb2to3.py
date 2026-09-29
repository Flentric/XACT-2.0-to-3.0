#!/usr/bin/env python3
"""Convert XACT 2.x wave banks (.xwb) to the XACT 3 format.

XACT 3 banks (tool version 46 / header version 44) are what XNA 3.x/4.0,
MonoGame, FNA and DirectXTK expect. Audio data is copied verbatim; only the
container is rewritten:

  * header gains dwHeaderVersion and the XACT3 segment order
    (BANKDATA, ENTRYMETADATA, SEEKTABLES, ENTRYNAMES, ENTRYWAVEDATA)
  * the mini wave format is re-packed into the XACT3 bit layout
  * byte-based loop regions (content version <= 38) become sample-based
  * missing durations are computed from the play region

Supported codecs: PCM (8/16-bit), MS-ADPCM, and XMA2 (content 39-41, copied as
is). XMA1 (Xbox 360 banks up to content version 38) cannot be converted
without re-encoding and is rejected.

Layout details follow vgmstream's xwb.c and DirectXTK's WaveBankReader.cpp.
"""

import argparse
import os
import struct
import sys
from dataclasses import dataclass

# Content (tool) version boundaries, as classified by vgmstream.
XACT1_1_MAX = 3
XACT2_0_MAX = 21   # 4 segments, 16-byte bank name
XACT2_1_MAX = 22   # 4 or 5 segments
XACT2_2_MAX = 34   # old mini-format bit layout (1-bit tag)
XACT2_3_MAX = 38   # loop regions in bytes, NAMES before EXTRA
XACT2_4_MAX = 41   # XACT3 layout minus dwHeaderVersion

XACT3_TOOL_VERSION = 46
XACT3_HEADER_VERSION = 44

TAG_PCM, TAG_XMA, TAG_ADPCM, TAG_WMA = 0, 1, 2, 3
TAG_NAMES = {TAG_PCM: "PCM", TAG_XMA: "XMA", TAG_ADPCM: "ADPCM", TAG_WMA: "xWMA"}
ADPCM_BLOCKALIGN_CONVERSION_OFFSET = 22

TYPE_STREAMING = 0x00000001
FLAGS_ENTRYNAMES = 0x00010000
FLAGS_COMPACT = 0x00020000
FLAGS_SEEKTABLES = 0x00080000

ENTRY_SIZE = 24
BANKDATA_SIZE = 96
HEADER_SIZE = 52
NAME_SIZE = 64
DVD_SECTOR_SIZE = 2048


class ConvertError(Exception):
    pass


@dataclass
class MiniFormat:
    tag: int
    channels: int
    rate: int
    block_align: int  # raw 8-bit field (ADPCM: per-channel align minus 22)
    bits: int         # 0 = 8-bit, 1 = 16-bit (PCM only)

    @classmethod
    def unpack(cls, value, version):
        if version <= XACT2_2_MAX:
            return cls(tag=value & 0x1,
                       channels=(value >> 1) & 0x7,
                       rate=(value >> 4) & 0x7FFFF,
                       block_align=(value >> 24) & 0x7F,
                       bits=(value >> 31) & 0x1)
        return cls(tag=value & 0x3,
                   channels=(value >> 2) & 0x7,
                   rate=(value >> 5) & 0x3FFFF,
                   block_align=(value >> 23) & 0xFF,
                   bits=(value >> 31) & 0x1)

    def pack(self):
        if self.rate > 0x3FFFF:
            raise ConvertError(f"sample rate {self.rate} does not fit the XACT3 format")
        return ((self.tag & 0x3)
                | (self.channels & 0x7) << 2
                | (self.rate & 0x3FFFF) << 5
                | (self.block_align & 0xFF) << 23
                | (self.bits & 0x1) << 31)

    def normalized(self, version):
        """Return the format as XACT3 expects it for this source version."""
        if self.channels == 0:
            raise ConvertError("entry has 0 channels")
        tag = self.tag
        if tag == TAG_XMA and version <= XACT2_3_MAX:
            raise ConvertError("XMA1 audio (Xbox 360, content version <= 38) cannot be "
                               "converted without re-encoding")
        if tag == TAG_WMA or (version <= XACT2_2_MAX and tag not in (TAG_PCM, TAG_XMA)):
            raise ConvertError(f"unsupported codec tag {tag} for content version {version}")
        fmt = MiniFormat(tag, self.channels, self.rate, self.block_align, self.bits)
        if tag == TAG_PCM:
            fmt.block_align = self.channels * (2 if self.bits else 1)
        elif tag == TAG_XMA:
            fmt.bits = 1
        return fmt

    # --- helpers mirroring DirectXTK's MINIWAVEFORMAT ---
    def adpcm_block_bytes(self):
        return (self.block_align + ADPCM_BLOCKALIGN_CONVERSION_OFFSET) * self.channels

    def adpcm_samples_per_block(self):
        return self.adpcm_block_bytes() * 2 // self.channels - 12

    def bytes_to_samples(self, nbytes):
        if self.tag == TAG_PCM:
            return nbytes // (self.channels * (2 if self.bits else 1))
        if self.tag == TAG_ADPCM:
            block = self.adpcm_block_bytes()
            samples = (nbytes // block) * self.adpcm_samples_per_block()
            partial = nbytes % block
            if partial >= 7 * self.channels:
                samples += partial * 2 // self.channels - 12
            return samples
        raise ConvertError(f"cannot compute sample positions for {TAG_NAMES[self.tag]}")


class Reader:
    def __init__(self, data, endian):
        self.data = data
        self.e = endian

    def u32(self, off):
        if off + 4 > len(self.data):
            raise ConvertError(f"unexpected end of file at 0x{off:x}")
        return struct.unpack_from(self.e + "I", self.data, off)[0]

    def blob(self, off, size):
        if size and (off < 0 or off + size > len(self.data)):
            raise ConvertError(f"segment 0x{off:x}+0x{size:x} runs past end of file")
        return self.data[off:off + size]


def align_up(value, alignment):
    return (value + alignment - 1) // alignment * alignment


def convert(data, tool_version=XACT3_TOOL_VERSION, log=lambda msg: None):
    if data[:4] == b"WBND":
        e = "<"
    elif data[:4] == b"DNBW":
        e = ">"
    else:
        raise ConvertError("not an XACT wave bank (missing WBND signature)")
    r = Reader(data, e)
    version = r.u32(4)

    if version > XACT2_4_MAX and version != 0x87:
        if 42 <= version <= 46 or version == 0x10000:
            raise ConvertError(f"bank is already XACT3 (content version {version})")
        raise ConvertError(f"unknown content version {version}")
    if version == 0x87:
        raise ConvertError("Crackdown-style banks (version 0x87) are not supported")
    if version <= XACT1_1_MAX:
        raise ConvertError(f"XACT1 banks (content version {version}) are not supported")

    # ---- header / segments ----
    def seg(i):
        return r.u32(8 + i * 8), r.u32(12 + i * 8)

    bank_off, bank_len = seg(0)
    meta_off, meta_len = seg(1)
    seek = (0, 0)
    extra = (0, 0)
    if version <= XACT2_0_MAX:
        names, wave = seg(2), seg(3)
    elif version <= XACT2_1_MAX:
        names = seg(2)
        if bank_off == 0x28:
            wave = seg(3)
        elif bank_off == 0x30:
            extra, wave = seg(3), seg(4)
        else:
            raise ConvertError("unrecognised v22 header layout")
    elif version <= XACT2_3_MAX:
        names, extra, wave = seg(2), seg(3), seg(4)
    else:
        seek, names, wave = seg(2), seg(3), seg(4)
    log(f"source: content version {version}, {'big' if e == '>' else 'little'}-endian")
    if extra[1]:
        log(f"note: dropping 0x{extra[1]:x}-byte legacy EXTRA segment (XMA1 data, unused here)")

    # ---- bank data ----
    name_len = 16 if version <= XACT2_0_MAX else 64
    flags = r.u32(bank_off)
    count = r.u32(bank_off + 4)
    bank_name = r.blob(bank_off + 8, name_len).split(b"\0", 1)[0]
    p = bank_off + 8 + name_len
    meta_elem = r.u32(p)
    name_elem = r.u32(p + 4)
    alignment = r.u32(p + 8)
    compact_fmt_raw = r.u32(p + 12)
    build_time = r.blob(p + 16, 8) if p + 24 <= bank_off + bank_len else b"\0" * 8
    streaming = bool(flags & TYPE_STREAMING)
    compact = bool(flags & FLAGS_COMPACT)
    log(f"bank '{bank_name.decode('latin-1')}': {count} entries, "
        f"{'streaming' if streaming else 'in-memory'}{', compact' if compact else ''}, "
        f"alignment {alignment}")

    min_align = DVD_SECTOR_SIZE if streaming else 4
    wave_data = r.blob(*wave)
    meta = r.blob(meta_off, meta_len)

    if compact:
        # Compact entries (21-bit sector offset / 11-bit deviation) are
        # unchanged in XACT3; keep the data segment as is.
        if meta_elem != 4:
            raise ConvertError(f"compact bank with entry size {meta_elem}")
        if alignment < min_align or (streaming and alignment % DVD_SECTOR_SIZE):
            raise ConvertError(f"compact bank alignment {alignment} is not valid for XACT3")
        cfmt = MiniFormat.unpack(compact_fmt_raw, version).normalized(version)
        compact_fmt_raw = cfmt.pack()
        new_meta = meta[:count * 4]
        out_align = alignment
        new_wave = wave_data
    else:
        if meta_elem < ENTRY_SIZE:
            raise ConvertError(f"unexpected entry size {meta_elem}")
        out_align = max(alignment, min_align)
        if streaming and out_align % DVD_SECTOR_SIZE:
            out_align = align_up(out_align, DVD_SECTOR_SIZE)
        new_meta = bytearray()
        new_wave = bytearray()
        for i in range(count):
            eo = meta_off + i * meta_elem
            flags_dur = r.u32(eo)
            fmt = MiniFormat.unpack(r.u32(eo + 4), version).normalized(version)
            play_off, play_len = r.u32(eo + 8), r.u32(eo + 12)
            loop_a, loop_b = r.u32(eo + 16), r.u32(eo + 20)
            if play_off + play_len > len(wave_data):
                raise ConvertError(f"entry {i}: play region outside wave data")

            entry_flags = flags_dur & 0xF
            duration = flags_dur >> 4
            if fmt.tag == TAG_PCM or (fmt.tag == TAG_ADPCM and duration == 0):
                duration = fmt.bytes_to_samples(play_len)
            if version <= XACT2_3_MAX and (loop_a or loop_b):
                # (byte offset, byte length) -> (start sample, sample count)
                start = fmt.bytes_to_samples(loop_a)
                end = fmt.bytes_to_samples(loop_a + loop_b)
                loop_a, loop_b = start, end - start
            if duration > 0x0FFFFFFF:
                raise ConvertError(f"entry {i}: duration too long for XACT3")

            new_off = align_up(len(new_wave), out_align)
            new_wave += b"\0" * (new_off - len(new_wave))
            new_wave += wave_data[play_off:play_off + play_len]
            new_meta += struct.pack(e + "6I", entry_flags | duration << 4, fmt.pack(),
                                    new_off, play_len, loop_a, loop_b)
            log(f"  #{i}: {TAG_NAMES[fmt.tag]} {fmt.channels}ch {fmt.rate}Hz, "
                f"{duration} samples" + (f", loop {loop_a}+{loop_b}" if loop_b else ""))
        meta_elem = ENTRY_SIZE

    seek_data = r.blob(*seek)
    if seek_data:
        flags |= FLAGS_SEEKTABLES
    else:
        flags &= ~FLAGS_SEEKTABLES

    names_data = r.blob(*names)
    if names_data:
        flags |= FLAGS_ENTRYNAMES
        if name_elem == 0:
            name_elem = NAME_SIZE
    else:
        flags &= ~FLAGS_ENTRYNAMES
        name_elem = 0

    # ---- assemble ----
    bankdata = struct.pack(e + "2I64s4I", flags, count, bank_name[:63], meta_elem,
                           name_elem, out_align, compact_fmt_raw) + build_time
    assert len(bankdata) == BANKDATA_SIZE

    body = bytearray()
    segments = []
    pos = HEADER_SIZE
    for idx, (blob, seg_align) in enumerate(((bankdata, 4), (new_meta, 4), (seek_data, 4),
                                             (names_data, 4), (new_wave, out_align))):
        start = align_up(pos, seg_align)
        body += b"\0" * (start - pos)
        body += blob
        # empty optional segments are (0, 0); wave data always gets an offset
        segments.append((start if blob or idx == 4 else 0, len(blob)))
        pos = start + len(blob)

    header = (data[:4] + struct.pack(e + "2I", tool_version, XACT3_HEADER_VERSION)
              + b"".join(struct.pack(e + "2I", o, n) for o, n in segments))
    assert len(header) == HEADER_SIZE
    return bytes(header + body)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Convert XACT 2.x .xwb wave banks to XACT 3.")
    ap.add_argument("input", nargs="+", help="XACT2 .xwb file(s)")
    ap.add_argument("-o", "--output",
                    help="output file (single input) or directory (multiple inputs); "
                         "default: <name>.xact3.xwb next to the input")
    ap.add_argument("--tool-version", type=int, default=XACT3_TOOL_VERSION,
                    help=f"XACT3 content version to write (default {XACT3_TOOL_VERSION}, "
                         "what XNA 4.0 expects)")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    log = (lambda m: None) if args.quiet else (lambda m: print(m, file=sys.stderr))
    failures = 0
    for path in args.input:
        if args.output and len(args.input) == 1 and not os.path.isdir(args.output):
            out = args.output
        else:
            root, ext = os.path.splitext(os.path.basename(path))
            out_dir = args.output or os.path.dirname(path)
            out = os.path.join(out_dir, f"{root}.xact3{ext or '.xwb'}")
        try:
            with open(path, "rb") as f:
                result = convert(f.read(), args.tool_version, log)
            with open(out, "wb") as f:
                f.write(result)
            log(f"{path} -> {out}")
        except (ConvertError, OSError) as exc:
            print(f"{path}: error: {exc}", file=sys.stderr)
            failures += 1
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
