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
is). For PC targets (--pc, --techland or a PC reference bank), Xbox 360 banks
are rewritten little-endian: 16-bit PCM is byte-swapped, and XMA1/XMA2 audio,
which PCs cannot play, is decoded to 16-bit PCM with vgmstream-cli.

Layout details follow vgmstream's xwb.c and DirectXTK's WaveBankReader.cpp.
"""

import argparse
import array
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
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

    def normalized(self, version, decoding=False):
        """Return the format as XACT3 expects it for this source version."""
        if self.channels == 0:
            raise ConvertError("entry has 0 channels")
        tag = self.tag
        if tag == TAG_XMA and version <= XACT2_3_MAX and not decoding:
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


@dataclass
class Reference:
    """Container settings copied from an existing XACT3 bank of the target game."""
    signature: bytes
    tool_version: int
    header_version: int
    bank_name: bytes
    streaming: bool
    alignment: int
    packed: bool      # entries stored back to back instead of on alignment boundaries
    count: int
    names: list
    tags: set

    @classmethod
    def parse(cls, data):
        e = "<" if data[:4] == b"WBND" else ">"
        if data[:4] not in (b"WBND", b"DNBW"):
            raise ConvertError("reference is not an XACT wave bank")
        r = Reader(data, e)
        tool_version, header_version = r.u32(4), r.u32(8)
        if not is_xact3(tool_version):
            raise ConvertError(f"reference is not an XACT3 bank (content version {tool_version})")
        segs = [(r.u32(12 + i * 8), r.u32(16 + i * 8)) for i in range(5)]
        bo = segs[0][0]
        flags, count = r.u32(bo), r.u32(bo + 4)
        meta_elem, name_elem, alignment = r.u32(bo + 72), r.u32(bo + 76), r.u32(bo + 80)
        packed = False
        tags = set()
        if not flags & FLAGS_COMPACT and meta_elem >= ENTRY_SIZE:
            for i in range(count):
                eo = segs[1][0] + i * meta_elem
                tags.add(r.u32(eo + 4) & 0x3)
                if alignment and r.u32(eo + 8) % alignment:
                    packed = True
        names = []
        if segs[3][1] and name_elem:
            blob = r.blob(*segs[3])
            names = [blob[i:i + name_elem].split(b"\0", 1)[0]
                     for i in range(0, len(blob), name_elem)]
        return cls(signature=data[:4], tool_version=tool_version,
                   header_version=header_version,
                   bank_name=r.blob(bo + 8, 64).split(b"\0", 1)[0],
                   streaming=bool(flags & TYPE_STREAMING), alignment=alignment,
                   packed=packed, count=count, names=names, tags=tags)


# Dead Island, Dead Island Riptide, Call of Juarez, Nail'd, ... (Chrome engine)
TECHLAND = Reference(signature=b"WBND", tool_version=0x10000, header_version=XACT3_HEADER_VERSION,
                     bank_name=b"", streaming=False, alignment=0, packed=True, count=0,
                     names=[], tags=set())


def is_xact3(version):
    return 42 <= version <= 46 or version == 0x10000


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


def find_vgmstream():
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ("vgmstream-cli.exe", "vgmstream-cli", "test.exe"):
        for folder in (here, os.path.join(here, "vgmstream")):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                return path
    return shutil.which("vgmstream-cli")


MISSING_VGMSTREAM = (
    "this bank contains Xbox 360 XMA audio, which must be decoded for PC. "
    "Download vgmstream-cli (https://vgmstream.org, 'Command-line (64-bit)' build) and "
    "put vgmstream-cli.exe and its DLLs next to xwb2to3.py")


def vgmstream_decode(exe, src, index):
    """Decode entry `index` of the bank file `src` to PCM16 with vgmstream-cli.
    Returns (channels, rate, pcm_bytes, (loop_start, loop_end) or None)."""
    if not exe:
        raise ConvertError(MISSING_VGMSTREAM)
    fd, out = tempfile.mkstemp(suffix=".wav", dir=os.path.dirname(src))
    os.close(fd)
    try:
        proc = subprocess.run([exe, "-i", "-I", "-s", str(index + 1), "-o", out, src],
                              capture_output=True, text=True)
        if proc.returncode != 0 or not os.path.getsize(out):
            raise ConvertError(f"vgmstream could not decode entry {index}: "
                               f"{(proc.stderr or proc.stdout).strip()[:300]}")
        info = {}
        for line in proc.stdout.splitlines():
            if line.startswith("{"):
                info = json.loads(line)
        with wave.open(out, "rb") as w:
            channels, rate = w.getnchannels(), w.getframerate()
            pcm = w.readframes(w.getnframes())
    finally:
        os.remove(out)
    loop = info.get("loopingInfo") or None
    return channels, rate, pcm, (loop["start"], loop["end"]) if loop else None


class VgmstreamDecoder:
    """Decodes entries of a bank held in memory with vgmstream-cli."""

    def __init__(self, exe=None):
        self.exe = exe or find_vgmstream()
        self.tmp = None
        self.src = None

    def bank_path(self, data):
        """Write the bank to a temporary file once and return its path."""
        if self.tmp is None:
            self.tmp = tempfile.TemporaryDirectory()
            self.src = os.path.join(self.tmp.name, "bank.xwb")
            with open(self.src, "wb") as f:
                f.write(data)
        return self.src

    def __call__(self, data, index):
        if not self.exe:
            raise ConvertError(MISSING_VGMSTREAM)
        return vgmstream_decode(self.exe, self.bank_path(data), index)

    def close(self):
        if self.tmp is not None:
            self.tmp.cleanup()
            self.tmp = None


ADPCM_COEFS = ((256, 0), (512, -256), (0, 0), (192, 64), (240, 0), (460, -208), (392, -232))
ADPCM_ADAPT = (230, 230, 230, 230, 307, 409, 512, 614, 768, 614, 512, 409, 307, 230, 230, 230)
ADPCM_ALIGN_FIELD = 48  # 70 bytes per channel = 128 samples per block, as XACT and Dead Island use
ADPCM_SAMPLES_PER_BLOCK = (ADPCM_ALIGN_FIELD + ADPCM_BLOCKALIGN_CONVERSION_OFFSET) * 2 - 12


def encode_msadpcm(pcm, channels, align_field=ADPCM_ALIGN_FIELD, chunk_blocks=2048,
                   best=False):
    """Encode 16-bit little-endian PCM to MS-ADPCM as XACT stores it.

    Blocks are independent, so all blocks are encoded in parallel with numpy.
    Each block uses the predictor that best fits its samples; with best=True
    all 7 predictors are fully encoded and the one with least error is kept
    (about 5x slower, marginally better). Returns (adpcm_bytes, sample_count)."""
    import numpy as np

    ch = channels
    block_bytes = (align_field + ADPCM_BLOCKALIGN_CONVERSION_OFFSET) * ch
    spb = block_bytes * 2 // ch - 12
    samples = np.frombuffer(pcm[:len(pcm) // (2 * ch) * 2 * ch], dtype="<i2").reshape(-1, ch)
    nblocks = max(1, -(-len(samples) // spb))
    padded = np.zeros((nblocks * spb, ch), dtype=np.int64)
    padded[:len(samples)] = samples
    blocks = padded.reshape(nblocks, spb, ch)

    coef1 = np.array([c[0] for c in ADPCM_COEFS], dtype=np.int64)[:, None, None]
    coef2 = np.array([c[1] for c in ADPCM_COEFS], dtype=np.int64)[:, None, None]
    adapt = np.array(ADPCM_ADAPT, dtype=np.int64)
    out = bytearray()
    for b0 in range(0, nblocks, chunk_blocks):
        x = blocks[b0:b0 + chunk_blocks].transpose(1, 0, 2)  # (spb, B, ch)
        nb_, nch = x.shape[1], x.shape[2]
        bidx = np.arange(nb_)[:, None]
        cidx = np.arange(nch)[None, :]
        if best:
            cand = np.broadcast_to(np.arange(7)[:, None, None], (7, nb_, nch))
        else:
            # open-loop fit: squared prediction error of each predictor on the real samples
            fit = np.stack([(((x[2:] - ((x[1:-1] * k1 + x[:-2] * k2) >> 8)) ** 2).sum(axis=0))
                            for k1, k2 in ADPCM_COEFS])  # (7, B, ch)
            cand = np.argmin(fit, axis=0)[None]           # (1, B, ch)
        c1, c2 = coef1[cand, 0, 0], coef2[cand, 0, 0]
        s2 = np.broadcast_to(x[0], cand.shape).copy()
        s1 = np.broadcast_to(x[1], cand.shape).copy()
        # initial step size from the prediction error of the first few samples
        n0 = min(spb, 6)
        err0 = np.zeros(cand.shape, dtype=np.int64)
        for t in range(2, n0):
            err0 += np.abs(x[t] - ((x[t - 1] * c1 + x[t - 2] * c2) >> 8))
        delta = np.clip(err0 // (2 * max(1, n0 - 2)), 16, 0x7FFF)  # int16 in the header
        delta0 = delta.copy()
        nibbles = np.empty((spb - 2,) + cand.shape, dtype=np.uint8)
        sse = np.zeros(cand.shape, dtype=np.int64)
        for t in range(2, spb):
            pred = (s1 * c1 + s2 * c2) >> 8
            nib = np.clip(np.floor_divide(x[t] - pred + (delta >> 1), delta), -8, 7)
            new = np.clip(pred + nib * delta, -32768, 32767)
            if best:
                sse += (x[t] - new) ** 2
            nib &= 0xF
            delta = np.maximum(16, (adapt[nib] * delta) >> 8)
            nibbles[t - 2] = nib
            s2, s1 = s1, new
        pick = np.argmin(sse, axis=0) if best else np.zeros((nb_, nch), dtype=np.int64)
        nb = nibbles[:, pick, bidx, cidx]                     # (spb-2, B, ch)
        nb = nb.transpose(1, 0, 2).reshape(nb_, -1)           # time-major, channel-minor
        packed = ((nb[:, 0::2] << 4) | nb[:, 1::2]).astype(np.uint8)
        header = np.concatenate([
            cand[pick, bidx, cidx].astype(np.uint8).reshape(-1, nch),
            delta0[pick, bidx, cidx].astype("<i2").view(np.uint8).reshape(-1, 2 * nch),
            x[1].astype("<i2").view(np.uint8).reshape(-1, 2 * nch),
            x[0].astype("<i2").view(np.uint8).reshape(-1, 2 * nch),
        ], axis=1)
        out += np.concatenate([header, packed], axis=1).tobytes()
    return bytes(out), nblocks * spb


def process_entry(job, decoder=None):
    """Decode, byte-swap and/or compress one entry. Runs in worker processes,
    so it only takes picklable data (`decoder` is for in-process use)."""
    fmt = MiniFormat(*job["fmt"])
    audio, duration = job["audio"], job["duration"]
    loop_a, loop_b = job["loop"]
    if job["decode"]:
        if decoder is not None:
            channels, rate, audio, loop = decoder(job["data"], job["index"])
        else:
            channels, rate, audio, loop = vgmstream_decode(job["exe"], job["src"], job["index"])
        fmt = MiniFormat(TAG_PCM, channels, rate, channels * 2, 1)
        duration = fmt.bytes_to_samples(len(audio))
        loop_a, loop_b = (loop[0], loop[1] - loop[0]) if loop else (0, 0)
    elif job["swap16"]:
        audio = swap16(audio)
    compressed = False
    if job["adpcm"] and fmt.tag == TAG_PCM and fmt.bits:
        audio, duration = encode_msadpcm(audio, fmt.channels, best=job["best"])
        fmt = MiniFormat(TAG_ADPCM, fmt.channels, fmt.rate, ADPCM_ALIGN_FIELD, 0)
        if loop_b:
            # XAudio2 wants ADPCM loops on block boundaries
            spb = ADPCM_SAMPLES_PER_BLOCK
            start = loop_a // spb * spb
            end = min(align_up(loop_a + loop_b, spb), duration)
            loop_a, loop_b = start, end - start
        compressed = True
    return ((fmt.tag, fmt.channels, fmt.rate, fmt.block_align, fmt.bits), audio, duration,
            (loop_a, loop_b), compressed)


def notes(*items):
    items = [i for i in items if i]
    return f" ({', '.join(items)})" if items else ""


def swap32_all(blob):
    words = array.array("I")
    words.frombytes(blob[:len(blob) // 4 * 4])
    words.byteswap()
    return words.tobytes()


def swap16(pcm):
    samples = array.array("h")
    samples.frombytes(pcm[:len(pcm) // 2 * 2])
    samples.byteswap()
    return samples.tobytes()


def align_up(value, alignment):
    return (value + alignment - 1) // alignment * alignment


def convert(data, tool_version=XACT3_TOOL_VERSION, log=lambda msg: None, like=None,
            pc=False, decoder=None, vgmstream=None, adpcm=True, best=False, executor=None):
    """Convert an XACT2 bank. `like` (a Reference) copies the target game's
    format: version numbers, byte order and whether entries are packed back
    to back. The bank's own name, streaming type and entries are kept.
    `pc` forces a little-endian bank with PC-playable audio; audio converted
    from an Xbox 360 bank is then compressed to MS-ADPCM unless `adpcm` is
    False. `decoder` turns
    an entry into PCM16 (defaults to vgmstream-cli at `vgmstream` or found
    automatically). `executor` (e.g. a ProcessPoolExecutor) runs the
    per-entry decoding/compression in parallel; `best` makes the ADPCM encoder
    try every predictor."""
    if data[:4] == b"WBND":
        e = "<"
    elif data[:4] == b"DNBW":
        e = ">"
    else:
        raise ConvertError("not an XACT wave bank (missing WBND signature)")
    r = Reader(data, e)
    version = r.u32(4)

    if version > XACT2_4_MAX and version != 0x87:
        if is_xact3(version):
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

    header_version = XACT3_HEADER_VERSION
    packed = False
    if like is not None:
        tool_version, header_version = like.tool_version, like.header_version
        packed = like.packed and not compact
        pc = pc or like.signature == b"WBND"
    out_e = "<" if pc else e
    swap = out_e != e
    if swap:
        log("converting Xbox 360 (big-endian) bank to PC (little-endian)")
        if compact:
            raise ConvertError("compact Xbox 360 banks cannot be converted for PC yet")
        if adpcm:
            try:
                import numpy  # noqa: F401
            except ImportError:
                log("warning: numpy is not installed, so audio is left as uncompressed PCM. "
                    "Run 'py -m pip install numpy' to enable ADPCM compression")
                adpcm = False

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
        entry_align = 1 if packed else out_align
        new_meta = bytearray()
        new_wave = bytearray()
        decoded_any = False
        own_decoder = decoder is None
        if own_decoder:
            decoder = VgmstreamDecoder(vgmstream)
        try:
            jobs = []
            for i in range(count):
                eo = meta_off + i * meta_elem
                flags_dur = r.u32(eo)
                raw_fmt = MiniFormat.unpack(r.u32(eo + 4), version)
                # PCs cannot play XMA; the ADPCM variant never shipped big-endian
                decode = raw_fmt.tag != TAG_PCM and (swap or pc and raw_fmt.tag == TAG_XMA)
                fmt = raw_fmt.normalized(version, decoding=decode)
                play_off, play_len = r.u32(eo + 8), r.u32(eo + 12)
                loop_a, loop_b = r.u32(eo + 16), r.u32(eo + 20)
                if play_off + play_len > len(wave_data):
                    raise ConvertError(f"entry {i}: play region outside wave data")
                duration = flags_dur >> 4
                audio = b""
                if not decode:
                    audio = wave_data[play_off:play_off + play_len]
                    if fmt.tag == TAG_PCM or (fmt.tag == TAG_ADPCM and duration == 0):
                        duration = fmt.bytes_to_samples(play_len)
                    if version <= XACT2_3_MAX and (loop_a or loop_b):
                        # (byte offset, byte length) -> (start sample, sample count)
                        start = fmt.bytes_to_samples(loop_a)
                        end = fmt.bytes_to_samples(loop_a + loop_b)
                        loop_a, loop_b = start, end - start
                decoded_any |= decode
                jobs.append(dict(
                    index=i, flags=flags_dur & 0xF, decode=decode, audio=audio,
                    fmt=(fmt.tag, fmt.channels, fmt.rate, fmt.block_align, fmt.bits),
                    duration=duration, loop=(loop_a, loop_b),
                    swap16=swap and fmt.tag == TAG_PCM and fmt.bits == 1,
                    adpcm=swap and adpcm, best=best))

            for j in jobs:
                j["heavy"] = bool(j["decode"] or j["adpcm"] or j["swap16"])
            heavy = [j for j in jobs if j["heavy"]]
            if decoded_any and own_decoder:
                if not decoder.exe:
                    raise ConvertError(MISSING_VGMSTREAM)
                src = decoder.bank_path(data)
                for j in heavy:
                    j.update(exe=decoder.exe, src=src)
            if executor is not None and own_decoder and len(heavy) > 1:
                futures = [executor.submit(process_entry, j) for j in heavy]
                results = {j["index"]: f for j, f in zip(heavy, futures)}
                get = lambda j: results[j["index"]].result()  # noqa: E731
            else:
                for j in heavy:
                    j["data"] = data
                get = lambda j: process_entry(j, None if own_decoder else decoder)  # noqa: E731

            for j in jobs:
                i = j["index"]
                if j["heavy"]:
                    fmt_t, audio, duration, (loop_a, loop_b), compressed = get(j)
                else:
                    fmt_t, audio, duration, (loop_a, loop_b), compressed = (
                        j["fmt"], j["audio"], j["duration"], j["loop"], False)
                fmt = MiniFormat(*fmt_t)
                if duration > 0x0FFFFFFF:
                    raise ConvertError(f"entry {i}: duration too long for XACT3")
                if like is not None and like.tags and fmt.tag not in like.tags:
                    log(f"warning: entry {i} is {TAG_NAMES[fmt.tag]} but the reference uses "
                        + "/".join(TAG_NAMES[t] for t in sorted(like.tags)))
                new_off = align_up(len(new_wave), entry_align)
                new_wave += b"\0" * (new_off - len(new_wave))
                new_wave += audio
                new_meta += struct.pack(out_e + "6I", j["flags"] | duration << 4, fmt.pack(),
                                        new_off, len(audio), loop_a, loop_b)
                log(f"  #{i}: {TAG_NAMES[fmt.tag]} {fmt.channels}ch {fmt.rate}Hz, "
                    f"{duration} samples" + (f", loop {loop_a}+{loop_b}" if loop_b else "")
                    + notes(j["decode"] and "decoded", compressed and "compressed to ADPCM"))
        finally:
            if own_decoder:
                decoder.close()
        meta_elem = ENTRY_SIZE

    seek_data = r.blob(*seek)
    if not compact and decoded_any:
        seek_data = b""  # XMA seek tables; the decoded audio is PCM
    elif swap and seek_data:
        seek_data = swap32_all(seek_data)
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
    bankdata = (struct.pack(out_e + "2I64s4I", flags, count, bank_name[:63], meta_elem,
                            name_elem, out_align, compact_fmt_raw)
                + struct.pack(out_e + "2I", *struct.unpack(e + "2I", build_time)))
    assert len(bankdata) == BANKDATA_SIZE

    body = bytearray()
    segments = []
    pos = HEADER_SIZE
    wave_align = 4 if packed else out_align
    for blob, seg_align in ((bankdata, 4), (new_meta, 4), (seek_data, 4),
                            (names_data, 4), (new_wave, wave_align)):
        start = align_up(pos, seg_align)
        body += b"\0" * (start - pos)
        body += blob
        # like the XACT tool, empty segments still point at the current position
        segments.append((start, len(blob)))
        pos = start + len(blob)

    signature = b"WBND" if out_e == "<" else b"DNBW"
    header = (signature + struct.pack(out_e + "2I", tool_version, header_version)
              + b"".join(struct.pack(out_e + "2I", o, n) for o, n in segments))
    assert len(header) == HEADER_SIZE
    return bytes(header + body)


def read_version(path):
    with open(path, "rb") as f:
        head = f.read(8)
    if len(head) < 8 or head[:4] not in (b"WBND", b"DNBW"):
        return None
    return struct.unpack(("<" if head[:4] == b"WBND" else ">") + "I", head[4:])[0]


def expand_inputs(paths):
    """Yield (file, output_dir) pairs. Folders are searched recursively for .xwb
    files, whose output mirrors the folder layout under <folder>/converted."""
    for path in paths:
        if not os.path.isdir(path):
            yield path, os.path.join(os.path.dirname(path), "converted")
            continue
        root = os.path.abspath(path)
        for folder, dirs, files in os.walk(root):
            dirs[:] = sorted(d for d in dirs if d.lower() != "converted")
            for name in sorted(files):
                if name.lower().endswith(".xwb"):
                    rel = os.path.relpath(folder, root)
                    yield (os.path.join(folder, name),
                           os.path.normpath(os.path.join(root, "converted", rel)))


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Convert XACT 2.x .xwb wave banks to XACT 3. If one of the given files is "
                    "already an XACT3 bank (e.g. the game's original), it is used as --like.")
    ap.add_argument("input", nargs="+",
                    help="XACT2 .xwb files and/or folders (searched recursively)")
    ap.add_argument("-o", "--output",
                    help="output file (single input) or directory; default: a 'converted' "
                         "folder next to each input, keeping the file name")
    ap.add_argument("--like", metavar="GAME_BANK.xwb",
                    help="any XACT3 bank from the target game; copies its format (version "
                         "numbers and data packing). Your bank keeps its own name and settings")
    ap.add_argument("--techland", action="store_true",
                    help="write the format used by Techland games such as Dead Island "
                         "(same as --like with one of their banks)")
    ap.add_argument("--pc", action="store_true",
                    help="make the bank playable on PC: little-endian, XMA decoded to PCM "
                         "(implied by --techland and by a PC --like bank)")
    ap.add_argument("--pcm", action="store_true",
                    help="with --pc: keep Xbox 360 audio as uncompressed 16-bit PCM instead of "
                         "compressing it to MS-ADPCM (bigger files, lossless)")
    ap.add_argument("--best", action="store_true",
                    help="slower ADPCM encoding that tries every predictor (marginally better)")
    ap.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1,
                    help="CPU cores to use for decoding/compressing (default: all)")
    ap.add_argument("--vgmstream", metavar="PATH",
                    help="vgmstream-cli executable used to decode XMA (default: next to this "
                         "script or on PATH)")
    ap.add_argument("--tool-version", type=int, default=XACT3_TOOL_VERSION,
                    help=f"XACT3 content version to write (default {XACT3_TOOL_VERSION}, "
                         "what XNA 4.0 expects; ignored with --like/--techland)")
    ap.add_argument("-q", "--quiet", action="store_true")
    args = ap.parse_args(argv)

    say = (lambda m: None) if args.quiet else (lambda m: print(m, file=sys.stderr, flush=True))
    like_path = args.like
    explicit = [p for p in args.input if not os.path.isdir(p)]
    if like_path is None:
        refs = []
        for path in explicit:
            try:
                version = read_version(path)
            except OSError:
                continue
            if version is not None and is_xact3(version):
                refs.append(path)
        if len(refs) > 1:
            print("error: more than one XACT3 bank given; only one reference bank is allowed",
                  file=sys.stderr)
            return 1
        if refs and len(args.input) > 1:
            like_path = refs[0]
    like = TECHLAND if args.techland else None
    if like_path:
        try:
            with open(like_path, "rb") as f:
                like = Reference.parse(f.read())
        except (ConvertError, OSError) as exc:
            print(f"{like_path}: error: {exc}", file=sys.stderr)
            return 1
    if like:
        say(f"target format: content version {like.tool_version}, "
            f"{'packed' if like.packed else 'aligned'} data"
            + (f" (from {like_path})" if like_path else " (Techland preset)"))

    tasks = []
    for path, out_dir in expand_inputs([p for p in args.input if p != like_path]):
        try:
            version = read_version(path)
        except OSError:
            version = None
        if version is not None and is_xact3(version):
            say(f"{path}: skipped, already XACT3")
            continue
        if args.output and len(args.input) == 1 and os.path.isfile(args.input[0]) \
                and not os.path.isdir(args.output):
            out = args.output
        else:
            out = os.path.join(args.output or out_dir, os.path.basename(path))
        tasks.append((path, out))
    if not tasks:
        print("error: no .xwb files to convert", file=sys.stderr)
        return 1

    def run(task, executor):
        path, out = task
        lines = []
        log = lines.append
        try:
            if os.path.exists(out) and any(os.path.samefile(out, p)
                                           for p in [path] + ([like_path] if like_path else [])):
                raise ConvertError(f"refusing to overwrite {out}")
            with open(path, "rb") as f:
                result = convert(f.read(), args.tool_version, log, like, pc=args.pc,
                                 vgmstream=args.vgmstream, adpcm=not args.pcm,
                                 best=args.best, executor=executor)
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            with open(out, "wb") as f:
                f.write(result)
            lines.append(f"{path} -> {out}")
            return True, lines
        except (ConvertError, OSError) as exc:
            lines.append(f"{path}: error: {exc}")
            return False, lines

    failures = 0
    jobs = max(1, args.jobs)
    if len(tasks) > 1:
        say(f"converting {len(tasks)} banks using {jobs} CPU core(s)")
    if jobs == 1:
        results = (run(t, None) for t in tasks)
        pool = files = None
    else:
        from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
        pool = ProcessPoolExecutor(max_workers=jobs)
        # a few banks at a time: their entries share the worker processes
        files = ThreadPoolExecutor(max_workers=min(jobs, len(tasks), 4))
        results = files.map(lambda t: run(t, pool), tasks)
    try:
        for n, (ok, lines) in enumerate(results, 1):
            failures += not ok
            for line in lines:
                if ok:
                    say(line)
                else:
                    print(line, file=sys.stderr, flush=True)
            if len(tasks) > 1:
                say(f"[{n}/{len(tasks)}] done")
    finally:
        if files:
            files.shutdown()
            pool.shutdown()
    if len(tasks) > 1:
        say(f"{len(tasks) - failures} converted, {failures} failed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
