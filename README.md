# XACT-2.0-to-3.0

`xwb2to3.py` converts XACT 2.x wave banks (`.xwb`) to the XACT 3 format. XACT 3 banks (tool version 46, header version 44) are what XNA 3.x/4.0, MonoGame, FNA and DirectXTK load.

Requires only Python 3.8+. There are no other dependencies.

## Usage

**Windows (drag and drop):** keep the `.bat` files in the same folder as `xwb2to3.py`. Drag one or more `.xwb` files onto `convert.bat`. Converted banks keep their file name and bank name, and are saved in a `converted` folder next to the originals. You need [Python 3](https://www.python.org/downloads/) installed.

**Dead Island and other Techland games:** drag your banks onto `convert_techland.bat` instead. Techland's engine expects its own XACT3 variant: content version `65536` instead of `46`, with sounds packed back to back.

**Xbox 360 banks for a PC game** (for example Saints Row → Dead Island): Xbox 360 banks are big-endian (reversed byte order) and usually hold XMA audio, which PCs can't play. For a PC target (`convert_techland.bat`, `--pc`, or a PC game bank as reference), the converter:

- rewrites the bank in little-endian (PC) byte order,
- byte-swaps 16-bit PCM audio,
- decodes XMA audio to 16-bit PCM with [vgmstream](https://vgmstream.org).

For the XMA step, download the vgmstream **command-line** build for Windows. Put `vgmstream-cli.exe` and its DLLs next to `xwb2to3.py`, or in a `vgmstream` subfolder. Decoded banks are larger than the originals, because PCM is uncompressed.

**Other games with a non-standard format:** drag any `.xwb` from the game onto `convert.bat` together with your banks. The converter spots that the game's file is already XACT3 and copies only its format: the version numbers and how the data is packed. Your banks keep their own names, streaming type and sounds.

**Command line:**

```sh
python3 xwb2to3.py "Wave Bank.xwb"                 # writes "converted/Wave Bank.xwb"
python3 xwb2to3.py in.xwb -o out.xwb
python3 xwb2to3.py banks/*.xwb -o converted/        # batch into a directory
python3 xwb2to3.py in.xwb --techland               # Dead Island / Techland format (PC)
python3 xwb2to3.py x360.xwb --pc                    # Xbox 360 bank -> standard PC XACT3
python3 xwb2to3.py in.xwb --like game_bank.xwb     # use the format of a game's own bank
python3 xwb2to3.py in.xwb --tool-version 45         # write a different XACT3 content version
```

## What it does

For banks that stay on the same platform, the tool copies the audio byte for byte and rewrites only the container:

- **Header:** adds `dwHeaderVersion` and reorders segments to the XACT3 layout (bank data, entry metadata, seek tables, names, wave data).
- **Wave format:** repacks each entry's mini wave format into the XACT3 bit layout. Content version ≤ 34 used a different layout.
- **Loop regions:** converts byte offsets (content version ≤ 38) to sample offsets.
- **Duration:** fills in missing entry durations, which some PC XACT2 ADPCM banks leave at 0.
- **Name:** widens a 16-byte bank name (content version ≤ 21) to 64 bytes.
- **Alignment:** realigns wave data where XACT3 needs it: at least 4 bytes, or 2048-byte sectors for streaming banks.

It supports XACT2 content versions 4–41, in little-endian (PC) and big-endian (Xbox 360) byte order. Output keeps the input's byte order unless the target is PC.

| Codec | Same platform | Xbox 360 → PC |
| --- | --- | --- |
| PCM 8/16-bit | yes | yes, byte-swapped |
| MS-ADPCM | yes | decoded to PCM with vgmstream |
| XMA2 (content 39–41) | copied as is | decoded to PCM with vgmstream |
| XMA1 (Xbox 360, content ≤ 38) | no | decoded to PCM with vgmstream |
| Compact banks | yes, if alignment is already valid | not yet |

## Tests

```sh
python3 -m unittest -v test_xwb2to3
VGMSTREAM=/path/to/vgmstream-cli python3 -m unittest -v test_xwb2to3   # also test real decoding
```

The tests build synthetic XACT2 banks and check the converted output against an XACT3 parser that follows DirectXTK's validation rules. Converted PCM and ADPCM banks were also checked with the vgmstream CLI: they decode to the same audio, with the same loop points, as the originals.
