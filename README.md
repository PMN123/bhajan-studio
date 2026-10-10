# Bhajan Studio

Pick any bhajan from [Sai Rhythms](https://sairhythms.org), choose your Sa (key), and Bhajan
Studio gets it ready for you to sing. It works out which key the original recording is really
sung in and pitch-shifts it to yours, keeping the singer's voice natural. It also reads the
Sai Samarpan sheet music note by note, transposes it, and plays it back on a flute (or a sampled
piano or harmonium), with an optional soft tanpura drone. You can follow along in sargam, in
transposed staff notation, or on the original sheet with every note outlined, and download WAV
or MIDI. It all runs on your own Mac.

## Quick start

You need a Mac with macOS 12 (Monterey) or later on Apple Silicon, or macOS 10.15 (Catalina) or
later on Intel, and an internet connection the first time. You don't need Homebrew, Xcode or your
own Python. (Tested on Apple Silicon. The bundled packages include Intel builds, but no one has
tried an Intel Mac yet; if you do, please open an issue either way.)

Keep the folder somewhere ordinary, such as your home folder (Finder → Go → Home), not inside an
iCloud-synced Desktop or Documents folder.

**Option A: Terminal**

```bash
git clone https://github.com/PMN123/bhajan-studio.git && cd bhajan-studio && ./start.sh
```

(If your Mac has never used `git`, macOS offers to install its command-line tools first. Accept,
or use option B instead.)

**Option B: download, then double-click**

1. On the GitHub page, click **Code → Download ZIP**. Double-click the ZIP to unzip it.
2. In the unzipped `bhajan-studio-main` folder, double-click **`start.command`**. A Terminal window
   opens and runs the app.
3. The first time, macOS blocks it because it came from the internet. To allow it:
   - **macOS 14 or earlier:** right-click (or Control-click) `start.command`, choose **Open**,
     then click **Open** again.
   - **macOS 15 or later:** click **Done**, open **System Settings → Privacy & Security**, scroll
     down, and click **Open Anyway** next to the `start.command` message. Then double-click it
     again.
   - **Or, in Terminal:** type `xattr -dr com.apple.quarantine` and a space, drag the
     `bhajan-studio-main` folder onto the Terminal window, press Return, then double-click
     `start.command`.
   - **Or skip the check entirely:** open Terminal, type `bash` and a space, drag `start.command`
     onto the window, and press Return.

   After the first run, double-clicking just works.

**What happens the first time:**

- If [uv](https://docs.astral.sh/uv/) isn't installed, the launcher explains what it is and asks
  `Install uv now? [Y/n]`. uv is a small free tool that runs Python apps. It installs into your
  home folder (`~/.local/bin`) from its official installer, with no administrator password. The
  installer also adds `~/.local/bin` to your shell's startup file (such as `~/.zshrc`), so `uv`
  works in new Terminal windows.
- uv downloads Python 3.12 and the app's packages: about 110 MB to download and about 400 MB on
  disk. These include ffmpeg, the Rubber Band pitch shifter and the macOS Vision bindings. This
  happens once and takes under a minute on a fast connection (a few minutes on a slow one). After
  that, the app starts in a few seconds.
- Your browser opens at <http://127.0.0.1:8777>.

The piano and harmonium are real recorded samples, downloaded the first time you choose them (about
3 MB each). The flute is synthesized and needs no download. Without internet, a synthesized piano
or harmonium plays instead.

**Going easy on Sai Rhythms.** The full song list ships with the app, so search works instantly
and is refreshed from the site at most once a week, in the background. Opening a song downloads
just its page and sheet music (about 0.6 MB, less than viewing it on sairhythms.org). A recording
(about 3 MB) is downloaded only when you click **Use the recording**. Everything is cached, so
nothing is fetched twice, and the app never sends more than two requests to the site at a time.

## Everyday use

- **Start:** double-click `start.command` (or run `./start.sh`).
- **Stop:** press `Ctrl+C` in the Terminal window, or just close the window.
- **Change the port:** run `./start.sh --port 9000`. If the port is already taken (by another app
  or a second copy of Bhajan Studio), the next free port is used and its address is printed.
  Nothing that is already running gets stopped.
- **Other options:** `--no-open` (don't open the browser), `--yes` (install uv without asking),
  `--no-install` (never install uv). Run `./start.sh --help` for the list.
- **Update:** run `git pull` in the folder. If you used the ZIP, download a fresh one. You can
  move the old `data` folder into the new one to keep your cache.
- **Cache:** everything the app downloads or works out goes in `data/` inside the folder. That
  includes the song index, song pages, recordings, sheet PDFs, read scores, instrument samples, and
  rendered and shifted audio. Rendered and shifted audio is kept to about 500 MB (the least recently
  used goes first). The index is refreshed weekly and song pages monthly. It's safe to delete
  `data/` at any time; things are fetched again as needed.
- **Uninstall:** delete the folder. To remove uv as well, follow
  [uv's uninstall steps](https://docs.astral.sh/uv/getting-started/installation/#uninstallation).

## How it works

| Piece | File | Notes |
|---|---|---|
| Launcher | `start.sh`, `start.command`, `server.py` | Checks the macOS version and for uv (offers to install it), then `uv run` sets up Python 3.12 and the locked dependencies in `.venv` and starts the local server. The server listens on 127.0.0.1 only, answers only requests addressed to 127.0.0.1/localhost that don't come from other websites, picks the next free port if 8777 is busy, and opens your browser when it's ready. |
| Sai Rhythms client | `bhajan/sairhythms.py` | Song index (3,600+ songs), song pages (lyrics, raga scale, reference pitches, range), recordings, and sheet PDFs. Everything is downloaded on demand and cached in `data/`. |
| Sheet reader (OMR) | `bhajan/omr.py`, `bhajan/sheet.py` | Built for the Sai Samarpan engraving. Pitch comes from staff position plus accidentals. Rhythm comes from stems, beams, flags, dots, ties and rests. The printed sargam labels are OCR'd with macOS Vision (called through pyobjc) and used to confirm or correct each note. Lyrics are attached from the blue text. A read in which OCR failed is not cached, so it is redone once OCR works. |
| Key detection | `bhajan/audio.py` | Audio is decoded with the ffmpeg build bundled in imageio-ffmpeg. A YIN pitch tracker builds the recording's pitch-class profile, which is matched at all 12 rotations against the sheet's melody (when available) and the raga's scale. |
| Pitch shift | `bhajan/audio.py` | The Rubber Band library through pylibrb, using its R3 ("finer") engine offline with formants preserved. The result is never louder than the original on average (RMS), and a gentle look-ahead limiter keeps peaks under −1 dBFS before MP3 encoding with the bundled ffmpeg. The limiter only acts on loudly mastered recordings (shifting brings back peaks their mastering had limited away), where it costs 1–2 dB of loudness. Shifts by the nearest interval by default, and can also correct the recording's tuning to A440. |
| Synth | `bhajan/synth.py`, `bhajan/instruments.py` | Synthesized flute (the default), recorded piano and harmonium samples (downloaded on first use, retuned, level-matched and looped), tanpura (Sa-Pa or Sa-Ma), and an optional click that follows the sheet's bars. Also writes MIDI with the matching General MIDI instrument. |
| UI | `static/` | Vanilla JS, with [abcjs](https://www.abcjs.net/) vendored for staff notation |

### Accuracy

Development set: 44 pages from 25+ songs in 4/4, 3/4, 7/8 and 5/4. Every bar's note values add up
(apart from one genuine pickup bar). In 2,066 of 2,069 notes, the OCR'd printed sargam label agrees
with the note read from the staff, and in the 3 disagreements the OCR was the one that was wrong.

Held-out check: 40 randomly chosen songs (639 bars), run end to end. 38 songs were fully
consistent and 2,610 of 2,615 labels agreed (99.8%). Of the two flagged songs, one genuinely
switches meter mid-song, and the other exposed a dotted-half-note bug that has since been fixed.
Each sheet shows its own check ("every bar adds up · N/N labels match"), and uncertain notes are
underlined.

Key detection was checked against the sheet melody: on 12 real recordings, the melody-based and
raga-based estimates agreed on 11. Recordings are often not at the published reference pitch
(three recordings of *Aao Aao Nandalala* are in G, F# and D), which is why each one is analysed.

Known limits: grace notes, triplets, repeat signs and mid-song tempo changes aren't interpreted,
so the piece plays straight through at one tempo. Key detection is very reliable when the song
has sheet music. Without sheet music, it relies on the raga scale alone.

### Developer tools

Run these from the repository folder. `uv run` uses the project's `.venv`, which has the Vision
bindings that the OCR needs.

- `uv run server.py [--port N] [--host H] [--open]` starts the server directly. The `PORT`
  environment variable sets the default port, and `BHAJAN_DATA` moves the cache folder. `--host`
  with anything other than 127.0.0.1 or localhost lets other devices on your network use the app,
  with no password, so the server prints a warning.
- `BHAJAN_OCR=auto|vision|tesseract|none` picks the OCR engine. `auto` (the default) uses Vision on
  macOS, else tesseract if it is on PATH, else no OCR; the sheet is then read from the notation
  alone, without the label, lyric and tempo hints.
- `uv run python tools/debug_omr.py page.png out.png` draws the detected notes on a rendered page.
- `uv run python tools/score_omr.py [--ocr vision|tesseract|none] pages/*.png` prints
  bar-consistency and label-agreement metrics.
- `uv run python tools/test_synth.py` checks all three instruments: timing, levels, clicks, key,
  and WAV/MIDI output. If the samples can't be downloaded, the sampled-instrument checks are
  skipped with a message.

## Acknowledgements & licenses

- **Bhajan Studio's own code** is MIT licensed. See [LICENSE](LICENSE).
- **[abcjs](https://www.abcjs.net/)** (MIT) draws the staff notation. It is vendored in
  `static/vendor/` with its license.
- **[Rubber Band Library](https://breakfastquay.com/rubberband/)** by Particular Programs Ltd.
  (GPL v2 or later) does the pitch shifting. It is used through
  [pylibrb](https://pypi.org/project/pylibrb/) (GPL v2) and installed from PyPI when you first
  run the app. It is not included in this repository.
- **[FFmpeg](https://ffmpeg.org/)** decodes and encodes audio. A GPL build ships inside
  [imageio-ffmpeg](https://github.com/imageio/imageio-ffmpeg) (BSD-2-Clause), which is installed
  from PyPI.
- **[PyMuPDF](https://pymupdf.readthedocs.io/)** (AGPL v3) renders the sheet PDFs, and
  **[PyObjC](https://pyobjc.readthedocs.io/)** (MIT) connects to Apple's Vision framework for OCR.
  The rest of the Python stack (FastAPI, Uvicorn, NumPy, SciPy, Pillow) is under permissive licenses.
- **Sharing the app.** Share this repository (or its ZIP) and let uv download the packages, as the
  launcher does. A ready-made copy that includes `.venv` or the bundled binaries (a zipped folder,
  an `.app`) contains GPL and AGPL software and may only be passed on under those licenses' terms.
- **Instrument samples.** The piano and harmonium samples come from the pre-rendered General MIDI
  soundfonts of the [MIDI.js Soundfonts](https://github.com/gleitz/midi-js-soundfonts) project by
  Benjamin Gleitzman. They are downloaded on first use (checksum-verified) into
  `data/instruments/` and are not included in this repository.
  - Harmonium: the "reed_organ" instrument of the Fluid (R3) General MIDI SoundFont
    (FluidR3_GM.sf2) by Frank Wen, licensed under
    [Creative Commons Attribution 3.0](https://creativecommons.org/licenses/by/3.0/us/)
    (source: <http://www.synthfont.com/SoundFonts/FluidR3_GM.sfArk>).
  - Piano: the "acoustic_grand_piano" instrument of the [FatBoy](https://fatboy.site) SoundFont
    (FatBoy.sf2), licensed under
    [Creative Commons Attribution-ShareAlike 3.0](https://creativecommons.org/licenses/by-sa/3.0/).
  - Bhajan Studio retunes, level-matches, loops and EQs these samples while rendering
    (`bhajan/instruments.py`). Audio rendered with the piano is an adaptation of CC BY-SA material.
- **Sai Rhythms content.** The bhajans, recordings, lyrics and Sai Samarpan sheet music belong
  to their owners ([sairhythms.org](https://sairhythms.org)). The sheet music is shared for
  non-commercial devotional singing, learning and teaching, and this tool is meant for that use
  only. Bhajan Studio downloads each item from Sai Rhythms when you ask for it and keeps it only
  in your local cache. The repository includes only a small song list
  (`bhajan/song_index.json.gz`: titles, ragas, beats and first lines) so search works without a
  download; it does not redistribute the recordings, sheet music, lyrics or meanings.
