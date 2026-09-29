# XACT-2.0-to-3.0

`xwb2to3.py` converts XACT 2.x wave banks (`.xwb`) to the XACT 3 format. XACT 3 banks (tool version 46, header version 44) are what XNA 3.x/4.0, MonoGame, FNA and DirectXTK load.

Requires Python 3.8+. Converting Xbox 360 banks for PC also uses [vgmstream](https://vgmstream.org) (to decode XMA) and numpy (to compress to ADPCM, installed with `py -m pip install numpy`).

## Usage

**Windows (drag and drop):** keep the `.bat` files in the same folder as `xwb2to3.py`. Drag `.xwb` files, or whole folders of them, onto `convert.bat`. Converted banks keep their file name and bank name, and are saved in a `converted` folder next to the originals. You need [Python 3](https://www.python.org/downloads/) installed.

**Dead Island and other Techland games:** drag your banks onto `convert_techland.bat` instead. Techland's engine expects its own XACT3 variant: content version `65536` instead of `46`, with sounds packed back to back.

**Xbox 360 banks for a PC game** (for example Saints Row → Dead Island): Xbox 360 banks are big-endian (reversed byte order) and usually hold XMA audio, which PCs can't play. For a PC target (`convert_techland.bat`, `--pc`, or a PC game bank as reference), the converter:

- rewrites the bank in little-endian (PC) byte order,
- byte-swaps 16-bit PCM audio,
- decodes XMA audio to 16-bit PCM with [vgmstream](https://vgmstream.org),
- compresses the audio to MS-ADPCM, the format Dead Island's own banks use. This is about 4× smaller than PCM.

For the XMA step, download the vgmstream **Command-line (64-bit)** build for Windows. Put `vgmstream-cli.exe` and its DLLs next to `xwb2to3.py`, or in a `vgmstream` subfolder.

The ADPCM encoder runs roughly 65× faster than real time per CPU core. Add `--best` to try every ADPCM predictor on each block; it's about 1.5× slower and only marginally better.

ADPCM compression needs numpy. Install it once with `py -m pip install numpy`. Without it, the audio stays as uncompressed PCM, which plays but is about 4× bigger. Pass `--pcm` to keep PCM on purpose.

**Other games with a non-standard format:** drag any `.xwb` from the game onto `convert.bat` together with your banks. The converter spots that the game's file is already XACT3 and copies only its format: the version numbers and how the data is packed. Your banks keep their own names, streaming type and sounds.

**Mass conversion:** drop a folder and every `.xwb` inside it, subfolders included, is converted. The results go into `<folder>\converted\`, keeping the same subfolder layout. Banks that are already XACT3 are skipped. The work is spread over all CPU cores: several banks are converted at once, and each track is decoded and compressed in parallel. Use `-j N` to limit the number of cores.

**Removing DJ talk and ads (`remove_ads.bat`):** drag already-converted banks, or folders of them, onto `remove_ads.bat`. It asks:

1. The minimum song length in seconds. The default is 90.
2. Whether to also drop tracks with a clearly lower sample rate than the bank's songs, meaning more than 2% lower. Small differences such as 47999 vs 48000 Hz are ignored.

It then lists every track with its length, sample rate, name, and whether it will be kept. Nothing is written until you confirm. Trimmed banks go into a `songs_only` folder, and everything else about each bank stays the same. Removing tracks shifts the positions of the tracks after them. From the command line: `python3 filter_xwb.py banks/ --min-seconds 90 [--top-rate] [--dry-run]`.

**Command line:**

```sh
python3 xwb2to3.py "Wave Bank.xwb"                 # writes "converted/Wave Bank.xwb"
python3 xwb2to3.py in.xwb -o out.xwb
python3 xwb2to3.py banks/*.xwb -o converted/        # batch into a directory
python3 xwb2to3.py sr1_audio/ --techland           # a whole folder, recursively
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
| PCM 8-bit | yes | yes |
| PCM 16-bit | yes | byte-swapped, then compressed to ADPCM |
| MS-ADPCM | yes | decoded with vgmstream, then compressed to ADPCM |
| XMA2 (content 39–41) | copied as is | decoded with vgmstream, then compressed to ADPCM |
| XMA1 (Xbox 360, content ≤ 38) | no | decoded with vgmstream, then compressed to ADPCM |
| Compact banks | yes, if alignment is already valid | not yet |

## Tests

```sh
python3 -m unittest -v test_xwb2to3 test_filter_xwb
VGMSTREAM=/path/to/vgmstream-cli python3 -m unittest -v test_xwb2to3   # also test real decoding
```

The tests build synthetic XACT2 banks and check the converted output against an XACT3 parser that follows DirectXTK's validation rules. Converted PCM and ADPCM banks were also checked with the vgmstream CLI: they decode to the same audio, with the same loop points, as the originals.
