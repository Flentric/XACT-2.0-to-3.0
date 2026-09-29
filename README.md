# XACT-2.0-to-3.0

`xwb2to3.py` converts XACT 2.x wave banks (`.xwb`) to the XACT 3 format. XACT 3 banks (tool version 46, header version 44) are what XNA 3.x/4.0, MonoGame, FNA and DirectXTK load.

Requires only Python 3.8+. There are no other dependencies.

## Usage

```sh
python3 xwb2to3.py "Wave Bank.xwb"                 # writes "Wave Bank.xact3.xwb"
python3 xwb2to3.py in.xwb -o out.xwb
python3 xwb2to3.py banks/*.xwb -o converted/        # batch into a directory
python3 xwb2to3.py in.xwb --tool-version 45         # write a different XACT3 content version
```

## What it does

The tool copies the audio data byte for byte and rewrites only the container:

- **Header:** adds `dwHeaderVersion` and reorders segments to the XACT3 layout (bank data, entry metadata, seek tables, names, wave data).
- **Wave format:** repacks each entry's mini wave format into the XACT3 bit layout. Content version ≤ 34 used a different layout.
- **Loop regions:** converts byte offsets (content version ≤ 38) to sample offsets.
- **Duration:** fills in missing entry durations, which some PC XACT2 ADPCM banks leave at 0.
- **Name:** widens a 16-byte bank name (content version ≤ 21) to 64 bytes.
- **Alignment:** realigns wave data where XACT3 needs it: at least 4 bytes, or 2048-byte sectors for streaming banks.

It supports XACT2 content versions 4–41, in both little-endian (PC) and big-endian (Xbox 360) byte order. Output keeps the input's byte order.

| Codec | Supported |
| --- | --- |
| PCM 8/16-bit | yes |
| MS-ADPCM | yes |
| XMA2 (content 39–41) | copied as is |
| XMA1 (Xbox 360, content ≤ 38) | no, it would need re-encoding |
| Compact banks | yes, if alignment is already valid |

## Tests

```sh
python3 -m unittest -v test_xwb2to3
```

The tests build synthetic XACT2 banks and check the converted output against an XACT3 parser that follows DirectXTK's validation rules. Converted PCM and ADPCM banks were also checked with the vgmstream CLI: they decode to the same audio, with the same loop points, as the originals.
