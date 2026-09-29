"""Tests for xwb2to3: build synthetic XACT2 banks, convert, re-parse as XACT3."""

import os
import struct
import unittest

import xwb2to3 as x


def old_fmt(tag, ch, rate, align, bits, version):
    if version <= x.XACT2_2_MAX:
        return tag | ch << 1 | rate << 4 | align << 24 | bits << 31
    return tag | ch << 2 | rate << 5 | align << 23 | bits << 31


def build_xact2(version, entries, names=None, flags=0, alignment=4, e="<",
                compact_entries=None, compact_fmt=0, extra=b""):
    """entries: list of (flags_dur, fmt, audio_bytes, loop_a, loop_b)."""
    name_len = 16 if version <= x.XACT2_0_MAX else 64
    if version <= x.XACT2_0_MAX:
        nseg = 4
    elif version <= x.XACT2_1_MAX:
        nseg = 5 if extra else 4
    else:
        nseg = 5
    hdr_len = 8 + nseg * 8

    wave = bytearray()
    if compact_entries is None:
        meta = bytearray()
        for fd, fmt, audio, la, lb in entries:
            off = x.align_up(len(wave), alignment)
            wave += b"\0" * (off - len(wave)) + audio
            meta += struct.pack(e + "6I", fd, fmt, off, len(audio), la, lb)
        meta_elem = 24
    else:
        meta = b"".join(struct.pack(e + "I", v) for v in compact_entries[0])
        wave = compact_entries[1]
        meta_elem = 4
    names_blob = b"".join(n.ljust(64, b"\0") for n in names) if names else b""
    if names:
        flags |= x.FLAGS_ENTRYNAMES

    bank = (struct.pack(e + "2I", flags, len(entries) if compact_entries is None
                        else len(compact_entries[0]))
            + b"TestBank".ljust(name_len, b"\0")
            + struct.pack(e + "4I", meta_elem, 64 if names else 0, alignment, compact_fmt)
            + b"\x11" * 8)

    layout = {"bank": bank, "meta": bytes(meta), "names": names_blob,
              "extra": extra, "seek": b"", "wave": bytes(wave)}
    if version <= x.XACT2_0_MAX or (version <= x.XACT2_1_MAX and not extra):
        order = ["bank", "meta", "names", "wave"]
    elif version <= x.XACT2_3_MAX:
        order = ["bank", "meta", "names", "extra", "wave"]
    else:
        order = ["bank", "meta", "seek", "names", "wave"]

    body = bytearray()
    segs = []
    pos = hdr_len
    for key in order:
        blob = layout[key]
        a = alignment if key == "wave" else 4
        start = x.align_up(pos, a)
        body += b"\0" * (start - pos) + blob
        segs.append((start if blob or key == "wave" else 0, len(blob)))
        pos = start + len(blob)
    sig = b"WBND" if e == "<" else b"DNBW"
    header = sig + struct.pack(e + "I", version) + b"".join(
        struct.pack(e + "2I", o, n) for o, n in segs)
    return header + bytes(body)


def parse_xact3(data, packed=False):
    """Independent XACT3 parser mirroring DirectXTK's WaveBankReader checks."""
    e = "<" if data[:4] == b"WBND" else ">"
    assert data[:4] in (b"WBND", b"DNBW")
    version, hver = struct.unpack_from(e + "2I", data, 4)
    assert hver == 44
    segs = [struct.unpack_from(e + "2I", data, 12 + 8 * i) for i in range(5)]
    bo, bl = segs[0]
    assert bl == 96
    flags, count = struct.unpack_from(e + "2I", data, bo)
    name = data[bo + 8:bo + 72].split(b"\0")[0]
    meta_elem, name_elem, alignment, cfmt = struct.unpack_from(e + "4I", data, bo + 72)
    if flags & x.TYPE_STREAMING:
        assert alignment >= 2048 and alignment % 2048 == 0
    else:
        assert 4 <= alignment <= 0xFFFF
    compact = bool(flags & x.FLAGS_COMPACT)
    assert meta_elem == (4 if compact else 24)
    assert segs[1][1] >= count * meta_elem
    wo, wl = segs[4]
    assert wo % alignment == 0
    wave = data[wo:wo + wl]
    assert len(wave) == wl
    names = []
    if segs[3][1]:
        assert flags & x.FLAGS_ENTRYNAMES
        for i in range(count):
            o = segs[3][0] + i * name_elem
            names.append(data[o:o + name_elem].split(b"\0")[0])
    entries = []
    for i in range(count):
        o = segs[1][0] + i * meta_elem
        if compact:
            entries.append(struct.unpack_from(e + "I", data, o)[0])
            continue
        fd, fmt, po, pl, ls, lt = struct.unpack_from(e + "6I", data, o)
        assert (packed or po % alignment == 0) and po + pl <= wl
        entries.append(dict(flags=fd & 0xF, duration=fd >> 4,
                            fmt=x.MiniFormat.unpack(fmt, 46),
                            audio=wave[po:po + pl], loop=(ls, lt)))
    return dict(version=version, flags=flags, name=name, alignment=alignment,
                entries=entries, names=names, cfmt=cfmt, wave=wave)


class ConvertTests(unittest.TestCase):
    def test_pcm_v37_with_loop_and_names(self):
        audio = bytes(range(256)) * 16  # 4096 bytes, 16-bit stereo = 1024 samples
        fmt = old_fmt(x.TAG_PCM, 2, 44100, 0, 1, 37)
        src = build_xact2(37, [(0 | (0 << 4), fmt, audio, 400, 2000)],
                          names=[b"music"], extra=b"")
        out = parse_xact3(x.convert(src))
        self.assertEqual(out["version"], 46)
        self.assertEqual(out["name"], b"TestBank")
        self.assertEqual(out["names"], [b"music"])
        ent = out["entries"][0]
        self.assertEqual(ent["audio"], audio)
        self.assertEqual(ent["duration"], 1024)
        self.assertEqual((ent["fmt"].tag, ent["fmt"].channels, ent["fmt"].rate,
                          ent["fmt"].block_align, ent["fmt"].bits),
                         (x.TAG_PCM, 2, 44100, 4, 1))
        self.assertEqual(ent["loop"], (100, 500))  # bytes / 4

    def test_old_bit_layout_v34_pcm8_mono(self):
        audio = b"\x80" * 1000
        fmt = old_fmt(x.TAG_PCM, 1, 22050, 0, 0, 34)
        src = build_xact2(34, [(0, fmt, audio, 0, 0), (3, fmt, audio[:10], 0, 0)])
        out = parse_xact3(x.convert(src))
        a, b = out["entries"]
        self.assertEqual((a["fmt"].rate, a["fmt"].channels, a["fmt"].bits,
                          a["fmt"].block_align), (22050, 1, 0, 1))
        self.assertEqual(a["duration"], 1000)
        self.assertEqual(b["flags"], 3)
        self.assertEqual(b["audio"], audio[:10])

    def test_adpcm_v37_missing_duration_and_loop(self):
        ch, align_field = 2, 48  # block = (48 + 22) * 2 = 140 bytes, 128 samples/block
        block = (align_field + 22) * ch
        spb = block * 2 // ch - 12
        audio = b"\x01" * (block * 10)
        fmt = old_fmt(x.TAG_ADPCM, ch, 44100, align_field, 0, 37)
        src = build_xact2(37, [(0, fmt, audio, block * 2, block * 5)])
        ent = parse_xact3(x.convert(src))["entries"][0]
        self.assertEqual(ent["fmt"].tag, x.TAG_ADPCM)
        self.assertEqual(ent["fmt"].block_align, align_field)
        self.assertEqual(ent["duration"], spb * 10)
        self.assertEqual(ent["loop"], (spb * 2, spb * 5))

    def test_v40_sample_loops_kept_and_duration_kept(self):
        ch, align_field = 1, 48
        block = (align_field + 22) * ch
        audio = b"\x02" * (block * 4)
        fmt = old_fmt(x.TAG_ADPCM, ch, 32000, align_field, 0, 40)
        src = build_xact2(40, [(1 | (500 << 4), fmt, audio, 10, 300)])
        ent = parse_xact3(x.convert(src))["entries"][0]
        self.assertEqual(ent["duration"], 500)
        self.assertEqual(ent["flags"], 1)
        self.assertEqual(ent["loop"], (10, 300))

    def test_v21_short_name_four_segments(self):
        audio = b"\x00\x01" * 50
        fmt = old_fmt(x.TAG_PCM, 1, 48000, 0, 1, 21)
        src = build_xact2(21, [(0, fmt, audio, 0, 0)], names=[b"a"])
        out = parse_xact3(x.convert(src))
        self.assertEqual(out["name"], b"TestBank")
        self.assertEqual(out["names"], [b"a"])
        self.assertEqual(out["entries"][0]["duration"], 50)

    def test_streaming_bank_realigned_to_sector(self):
        audio = b"\x05" * 3000
        fmt = old_fmt(x.TAG_PCM, 1, 44100, 0, 1, 37)
        src = build_xact2(37, [(0, fmt, audio, 0, 0), (0, fmt, audio, 0, 0)],
                          flags=x.TYPE_STREAMING, alignment=512)
        out = parse_xact3(x.convert(src))
        self.assertEqual(out["alignment"], 2048)
        self.assertTrue(all(en["audio"] == audio for en in out["entries"]))

    def test_big_endian(self):
        audio = b"\x12\x34" * 64
        fmt = old_fmt(x.TAG_PCM, 1, 44100, 0, 1, 37)
        src = build_xact2(37, [(0, fmt, audio, 0, 0)], e=">")
        out = parse_xact3(x.convert(src))
        self.assertEqual(out["entries"][0]["audio"], audio)
        self.assertEqual(out["entries"][0]["fmt"].rate, 44100)

    def test_compact_bank(self):
        fmt = old_fmt(x.TAG_PCM, 1, 22050, 0, 1, 37)
        wave = b"\x07" * 4096
        compact = ([0, 1 | (10 << 21)], wave)  # offsets in 2048-byte sectors
        src = build_xact2(37, [], flags=x.FLAGS_COMPACT, alignment=2048,
                          compact_entries=compact, compact_fmt=fmt)
        out = parse_xact3(x.convert(src))
        self.assertEqual(out["entries"], [0, 1 | (10 << 21)])
        self.assertEqual(out["wave"], wave)
        cf = x.MiniFormat.unpack(out["cfmt"], 46)
        self.assertEqual((cf.rate, cf.channels, cf.bits), (22050, 1, 1))

    def _reference(self, streaming=True, packed=True):
        """A Techland-style XACT3 reference bank (content version 0x10000)."""
        fmt = old_fmt(x.TAG_ADPCM, 1, 48000, 48, 0, 46)
        audio = b"\x03" * 70
        meta = struct.pack("<6I", 128 << 4, fmt, 0, 70, 0, 0) + \
            struct.pack("<6I", 128 << 4, fmt, 70 if packed else 2048, 70, 0, 0)
        bank = struct.pack("<2I", x.FLAGS_ENTRYNAMES | (1 if streaming else 0), 2) + \
            b"game_bank".ljust(64, b"\0") + struct.pack("<4I", 24, 64, 2048, 0) + b"\0" * 8
        names = b"a".ljust(64, b"\0") + b"b".ljust(64, b"\0")
        wave = audio + audio if packed else audio.ljust(2048, b"\0") + audio
        off = 52
        segs = []
        body = b""
        for blob in (bank, meta, b"", names, wave):
            segs.append((off, len(blob)))
            body += blob
            off += len(blob)
        return (b"WBND" + struct.pack("<2I", 0x10000, 44)
                + b"".join(struct.pack("<2I", *sg) for sg in segs) + body)

    def test_like_reference_copies_format_only(self):
        like = x.Reference.parse(self._reference())
        self.assertEqual((like.tool_version, like.bank_name, like.streaming, like.packed),
                         (0x10000, b"game_bank", True, True))
        ch, align_field = 1, 48
        audio = b"\x01" * 70 * 3
        fmt = old_fmt(x.TAG_ADPCM, ch, 48000, align_field, 0, 37)
        src = build_xact2(37, [(0, fmt, audio, 0, 0), (0, fmt, audio[:70], 0, 0)],
                          names=[b"a", b"b"], alignment=4)
        out_bytes = x.convert(src, like=like)
        self.assertEqual(struct.unpack_from("<2I", out_bytes, 4), (0x10000, 44))
        e = "<"
        segs = [struct.unpack_from(e + "2I", out_bytes, 12 + 8 * i) for i in range(5)]
        bo = segs[0][0]
        flags = struct.unpack_from(e + "I", out_bytes, bo)[0]
        # the bank keeps its own identity: name, in-memory type, alignment
        self.assertFalse(flags & x.TYPE_STREAMING)
        self.assertEqual(out_bytes[bo + 8:bo + 17], b"TestBank\0")
        self.assertEqual(struct.unpack_from(e + "I", out_bytes, bo + 80)[0], 4)
        # packed: second entry directly follows the first
        po2 = struct.unpack_from(e + "I", out_bytes, segs[1][0] + 24 + 8)[0]
        self.assertEqual(po2, len(audio))
        self.assertEqual(segs[2], (segs[1][0] + segs[1][1], 0))

    def test_like_aligned_reference_keeps_alignment(self):
        like = x.Reference.parse(self._reference(packed=False))
        self.assertFalse(like.packed)
        fmt = old_fmt(x.TAG_PCM, 1, 48000, 0, 1, 37)
        src = build_xact2(37, [(0, fmt, b"\0\1" * 10, 0, 0), (0, fmt, b"\0\1" * 10, 0, 0)])
        out = parse_xact3(x.convert(src, like=like))
        self.assertEqual(out["version"], 0x10000)
        self.assertEqual(out["alignment"], 4)
        self.assertEqual(out["entries"][1]["audio"], b"\0\1" * 10)

    def test_cli_techland_preset_keeps_file_name(self):
        import os
        import tempfile
        fmt = old_fmt(x.TAG_PCM, 1, 48000, 0, 1, 37)
        src = build_xact2(37, [(0, fmt, b"\0\1" * 3, 0, 0), (0, fmt, b"\2\3" * 3, 0, 0)])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "my_bank.xwb")
            with open(path, "wb") as f:
                f.write(src)
            self.assertEqual(x.main(["-q", "--techland", path]), 0)
            with open(os.path.join(tmp, "converted", "my_bank.xwb"), "rb") as f:
                out_bytes = f.read()
        out = parse_xact3(out_bytes, packed=True)
        self.assertEqual(out["version"], 0x10000)
        self.assertEqual(out["name"], b"TestBank")
        self.assertEqual([en["audio"] for en in out["entries"]], [b"\0\1" * 3, b"\2\3" * 3])

    def test_x360_pcm16_to_pc_is_byteswapped(self):
        audio_be = struct.pack(">4h", 1, -2, 300, -400)
        fmt = old_fmt(x.TAG_PCM, 2, 44100, 0, 1, 40)
        src = build_xact2(40, [(0, fmt, audio_be, 0, 0)], e=">", names=[b"song"])
        out_bytes = x.convert(src, like=x.TECHLAND)
        self.assertEqual(out_bytes[:4], b"WBND")
        out = parse_xact3(out_bytes, packed=True)
        self.assertEqual(out["version"], 0x10000)
        self.assertEqual(out["names"], [b"song"])
        ent = out["entries"][0]
        self.assertEqual(ent["audio"], struct.pack("<4h", 1, -2, 300, -400))
        self.assertEqual((ent["fmt"].channels, ent["fmt"].rate, ent["duration"]), (2, 44100, 2))

    def test_x360_xma_is_decoded_for_pc(self):
        calls = []
        pcm = struct.pack("<6h", 1, 2, 3, 4, 5, 6)

        def fake_decoder(data, index):
            calls.append(index)
            return 2, 48000, pcm, (1, 3) if index == 0 else None

        for version in (37, 40):  # XMA1 and XMA2
            calls.clear()
            fmt = old_fmt(x.TAG_XMA, 2, 48000, 0, 0, version)
            src = build_xact2(version, [(0, fmt, b"\xaa" * 2048, 0, 0),
                                        (0, fmt, b"\xbb" * 2048, 0, 0)], e=">")
            out = parse_xact3(x.convert(src, pc=True, decoder=fake_decoder))
            self.assertEqual(calls, [0, 1])
            a, b = out["entries"]
            self.assertEqual((a["fmt"].tag, a["fmt"].channels, a["fmt"].block_align),
                             (x.TAG_PCM, 2, 4))
            self.assertEqual(a["audio"], pcm)
            self.assertEqual(a["duration"], 3)
            self.assertEqual(a["loop"], (1, 2))
            self.assertEqual(b["loop"], (0, 0))
            self.assertFalse(out["flags"] & x.FLAGS_SEEKTABLES)

    def test_x360_bank_without_pc_target_keeps_byte_order(self):
        fmt = old_fmt(x.TAG_PCM, 1, 44100, 0, 1, 40)
        src = build_xact2(40, [(0, fmt, b"\x00\x01", 0, 0)], e=">")
        out_bytes = x.convert(src)
        self.assertEqual(out_bytes[:4], b"DNBW")
        self.assertEqual(parse_xact3(out_bytes)["entries"][0]["audio"], b"\x00\x01")

    @unittest.skipUnless(os.environ.get("VGMSTREAM") or x.find_vgmstream(),
                         "vgmstream-cli not available")
    def test_vgmstream_decoder_matches_byteswap(self):
        audio_be = b"".join(struct.pack(">h", (i * 37) % 3000 - 1500) for i in range(2000))
        fmt = old_fmt(x.TAG_PCM, 2, 44100, 0, 1, 40)
        src = build_xact2(40, [(0, fmt, audio_be, 0, 0)], e=">")
        dec = x.VgmstreamDecoder(os.environ.get("VGMSTREAM"))
        try:
            channels, rate, pcm, loop = dec(src, 0)
        finally:
            dec.close()
        self.assertEqual((channels, rate, loop), (2, 44100, None))
        self.assertEqual(pcm, x.swap16(audio_be))

    def test_rejects_xma1_and_xact3(self):
        fmt = old_fmt(x.TAG_XMA, 2, 44100, 0, 0, 37)
        src = build_xact2(37, [(0, fmt, b"\0" * 2048, 0, 0)])
        with self.assertRaisesRegex(x.ConvertError, "XMA1"):
            x.convert(src)
        converted = x.convert(build_xact2(37, [(0, old_fmt(0, 1, 8000, 0, 1, 37),
                                                b"\0\0", 0, 0)]))
        with self.assertRaisesRegex(x.ConvertError, "already XACT3"):
            x.convert(converted)
        with self.assertRaisesRegex(x.ConvertError, "signature"):
            x.convert(b"RIFF\0\0\0\0")


if __name__ == "__main__":
    unittest.main()
