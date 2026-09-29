"""Tests for filter_xwb: drop short entries from converted XACT3 banks."""

import os
import tempfile
import unittest

import filter_xwb as f
import xwb2to3 as x
from test_xwb2to3 import build_xact2, old_fmt, parse_xact3


def converted_bank(lengths, rates=None, like=x.TECHLAND):
    """An XACT3 bank as xwb2to3 writes it; entry k is `lengths[k]` seconds of PCM."""
    rates = rates or [100] * len(lengths)
    entries = [(0, old_fmt(x.TAG_PCM, 1, rate, 0, 1, 40), bytes([k + 1, 0]) * int(sec * rate), 0, 0)
               for k, (sec, rate) in enumerate(zip(lengths, rates))]
    names = [f"track{k}".encode() for k in range(len(lengths))]
    return x.convert(build_xact2(40, entries, names=names), like=like)


class FilterTests(unittest.TestCase):
    def test_nothing_removed_is_identical(self):
        for like in (x.TECHLAND, None):
            data = converted_bank([5, 200, 30], like=like)
            bank = f.read_bank(data)
            self.assertEqual(f.write_bank(bank, bank["entries"]), data)

    def test_short_entries_removed(self):
        data = converted_bank([20, 200, 45, 180])
        bank = f.read_bank(data)
        kept, removed = f.choose(bank["entries"], 90, False)
        self.assertEqual([e["index"] for e in kept], [1, 3])
        out = f.write_bank(bank, kept)
        self.assertEqual(out[:12], data[:12])  # same signature and version numbers
        parsed = parse_xact3(out, packed=True)
        self.assertEqual(parsed["names"], [b"track1", b"track3"])
        self.assertEqual([e["audio"] for e in parsed["entries"]],
                         [bytes([2, 0]) * 20000, bytes([4, 0]) * 18000])
        self.assertEqual([e["duration"] for e in parsed["entries"]], [20000, 18000])

    def test_top_rate_tolerates_small_differences(self):
        data = converted_bank([200, 200, 200], rates=[120, 119, 60])
        bank = f.read_bank(data)
        kept, removed = f.choose(bank["entries"], 90, True)
        self.assertEqual([e["index"] for e in kept], [0, 1])
        self.assertEqual([e[0]["index"] for e in removed], [2])

    def test_aligned_bank_stays_aligned(self):
        data = converted_bank([100, 3, 100], like=None)
        bank = f.read_bank(data)
        self.assertFalse(bank["packed"])
        kept, _ = f.choose(bank["entries"], 90, False)
        parsed = parse_xact3(f.write_bank(bank, kept))  # strict alignment checks
        self.assertEqual(len(parsed["entries"]), 2)

    def test_cli_folder_dry_run_and_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "radio", "sub"))
            for rel in ("a.xwb", os.path.join("sub", "b.xwb")):
                with open(os.path.join(tmp, "radio", rel), "wb") as fh:
                    fh.write(converted_bank([10, 120]))
            with open(os.devnull, "w") as null:
                import contextlib
                with contextlib.redirect_stdout(null):
                    self.assertEqual(f.main(["--dry-run", os.path.join(tmp, "radio")]), 0)
                    self.assertFalse(os.path.exists(os.path.join(tmp, "radio", "songs_only")))
                    self.assertEqual(f.main([os.path.join(tmp, "radio")]), 0)
            for rel in ("a.xwb", os.path.join("sub", "b.xwb")):
                with open(os.path.join(tmp, "radio", "songs_only", rel), "rb") as fh:
                    self.assertEqual(len(parse_xact3(fh.read(), packed=True)["entries"]), 1)

    def test_rejects_xact2(self):
        src = build_xact2(40, [(0, old_fmt(x.TAG_PCM, 1, 100, 0, 1, 40), b"\0\0", 0, 0)])
        with self.assertRaisesRegex(x.ConvertError, "convert it with xwb2to3.py first"):
            f.read_bank(src)


if __name__ == "__main__":
    unittest.main()
