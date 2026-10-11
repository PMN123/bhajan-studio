# Bhajan Studio

Pick any bhajan from [Sai Rhythms](https://sairhythms.org), choose your Sa (key), and Bhajan
Studio gets it ready for you to sing. It works out which key the original recording is really
sung in and pitch-shifts it to yours, keeping the singer's voice natural. It also reads the
Sai Samarpan sheet music note by note, transposes it, and plays it back on a flute (or a sampled
piano or harmonium), with an optional soft tanpura drone. You can follow along in sargam, in
transposed staff notation, or on the original sheet with every note outlined, and download WAV
or MIDI. It runs on your own computer (a Mac, Windows or Linux), or on a server that your whole
centre can use.

## Quick start

You need an internet connection the first time. You don't need your own Python: the launcher sets
everything up inside the folder.

- [macOS](#macos)
- [Windows](#windows)
- [Linux](#linux)
- [Install it as an app](#install-it-as-an-app) (a window and an icon of its own)
- [Run it on a server (Docker)](#run-it-on-a-server-docker)

### macOS

You need macOS 12 (Monterey) or later on Apple Silicon, or macOS 10.15 (Catalina) or later on
Intel. You don't need Homebrew or Xcode. (Tested on Apple Silicon. The bundled packages include
Intel builds, but no one has tried an Intel Mac yet; if you do, please open an issue either way.)

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

### Windows

You need Windows 10 or 11 on a 64-bit Intel or AMD PC. (The app has been checked on Windows by its
automated tests, not yet by hand; if you try it, please open an issue either way.)

1. On the GitHub page, click **Code → Download ZIP**. Right-click the ZIP and choose
   **Extract All**. (Or, with Git: `git clone https://github.com/PMN123/bhajan-studio.git`.)
2. In the extracted `bhajan-studio-main` folder, double-click **`start.bat`**. A console window
   opens and runs the app.
3. If Windows asks whether to run it ("Windows protected your PC", or "Open File – Security
   Warning"), choose **More info → Run anyway**, or **Run**. To avoid the question, right-click the
   ZIP before extracting it, choose **Properties**, tick **Unblock** and click **OK**.

The first time, the launcher offers to install uv (into your user folder, no administrator rights
needed), and uv downloads Python 3.12 and the app's packages (about 100 MB). Then your browser
opens at <http://127.0.0.1:8777>.

Reading the printed sargam labels and lyrics on the sheet music (OCR) uses
[Tesseract](https://github.com/tesseract-ocr/tesseract) on Windows. It is optional: without it, the
sheet is read from the notation alone. To add it, run `winget install UB-Mannheim.TesseractOCR` in a
terminal (or use [its installer](https://github.com/UB-Mannheim/tesseract/wiki)), then restart
Bhajan Studio.

### Linux

On a 64-bit Intel or AMD machine (Ubuntu, Debian, Fedora and the like):

```bash
git clone https://github.com/PMN123/bhajan-studio.git && cd bhajan-studio && ./start.sh
```

The launcher works as on macOS: it offers to install uv, uv sets up Python 3.12 and the packages
(about 100 MB), and your browser opens. For OCR, install Tesseract: `sudo apt install tesseract-ocr`
(Debian, Ubuntu) or `sudo dnf install tesseract` (Fedora). On 64-bit ARM Linux (a Raspberry Pi 5,
say), the Rubber Band package has to be compiled there; use the [Docker image](#run-it-on-a-server-docker),
which does that for you.

### Good to know

The piano and harmonium are real recorded samples, downloaded the first time you choose them (about
3 MB each). The flute is synthesized and needs no download. Without internet, a synthesized piano
or harmonium plays instead.

**Going easy on Sai Rhythms.** The full song list ships with the app, so search works instantly
and is refreshed from the site at most once a week, in the background. Opening a song downloads
just its page and sheet music (about 0.6 MB, less than viewing it on sairhythms.org). A recording
(about 3 MB) is downloaded only when you click **Use the recording**. Everything is cached, so
nothing is fetched twice, and the app never sends more than two requests to the site at a time.

### Install it as an app

Once Bhajan Studio is open in your browser, you can install it, so it gets its own window and an
icon in the Dock, taskbar or Start menu:

- **Chrome or Edge** (macOS, Windows, Linux): click the install icon at the right end of the
  address bar, or choose **Install Bhajan Studio** in the browser's menu (in Chrome it is under
  **Cast, save and share**).
- **Safari** (macOS 14 or later): **File → Add to Dock**.

The installed app is a window onto the Bhajan Studio server, so start that first, as usual (with
`start.command`, `start.bat` or `./start.sh`), and keep the default port: the app remembers its
address, <http://127.0.0.1:8777>.

On a phone or tablet, install it from a [server](#run-it-on-a-server-docker) with an HTTPS address
(browsers only install apps from `https://` addresses, or from the same computer): in Safari on
iPhone or iPad, **Share → Add to Home Screen**; in Chrome on Android, **⋮ → Add to Home screen**.

## Everyday use

- **Start:** double-click `start.command` (macOS) or `start.bat` (Windows), or run `./start.sh`.
- **Stop:** press `Ctrl+C` in the Terminal or console window, or just close the window.
- **Change the port:** run `./start.sh --port 9000` (on Windows, `start.bat -Port 9000` in a
  terminal). If the port is already taken (by another app or a second copy of Bhajan Studio), the
  next free port is used and its address is printed. Nothing that is already running gets stopped.
- **Other options:** `--no-open` (don't open the browser), `--yes` (install uv without asking),
  `--no-install` (never install uv). Run `./start.sh --help` for the list. On Windows they are
  `-NoOpen`, `-Yes`, `-NoInstall` and `-Help`.
- **Update:** run `git pull` in the folder. If you used the ZIP, download a fresh one. You can
  move the old `data` folder into the new one to keep your cache.
- **Cache:** everything the app downloads or works out goes in `data/` inside the folder. That
  includes the song index, song pages, recordings, sheet PDFs, read scores, instrument samples, and
  rendered and shifted audio. Rendered and shifted audio is kept to about 500 MB (the least recently
  used goes first). Split parts (`data/stems`) are kept to about 3 GB the same way, and the
  separation models (`data/models`, about 1.6 GB) stay until you delete them. The index is
  refreshed weekly and song pages monthly. It's safe to delete `data/` at any time; things are
  fetched again as needed.
- **Uninstall:** delete the folder. To remove uv as well, follow
  [uv's uninstall steps](https://docs.astral.sh/uv/getting-started/installation/#uninstallation).

### Parts (singing, tabla, harmonium)

After **Use the recording**, **Split into parts** separates it into the singing (the lead and the
group that answers, together), tabla (with manjira and other percussion), harmonium (with the other
melody instruments) and a small remainder. You can then mute or rebalance the parts in your key, at
85–115% speed, hear the mix straight away, and download it as an MP3. With every part at 0 dB you
hear the original recording. The presets follow what a centre is missing:

- **Music only:** no instrumentalists. Tabla and harmonium (in the gents' key, say) for the hall to
  sing over.
- **Singing only:** no singers, or to learn the tune; the centre's own musicians play along.
- **Without tabla:** the centre has a tablist but no harmonium player.
- **Without harmonium:** the centre has a harmonium player but no tablist.

**Weekly session:** **Add to session** collects bhajans, each with its own key, speed and mix.
**Export session** renders them into one MP3, with a few seconds of silence between bhajans, and
lists where each one starts. Every bhajan is set to the same loudness as a full recording, and a
mix without the singers stays as much quieter as the music sits under them, so the hall's voices
still carry over it.

Recordings Sai Rhythms tags *golden voice* (Swami's own voice) are not split. For bhajans it tags
*karaoke* or *instrumental*, the card points to that official recording first: where it exists, it
beats any split.

**The first split** installs PyTorch and the separation runner (about 1 GB, into `.venv`; on Linux
PyTorch also brings NVIDIA's CUDA libraries, several GB more) and downloads two separation models
(about 1.6 GB, into `data/models`), so allow time for that on a slow connection. After that, a split
takes about 1–3 minutes per bhajan on Apple Silicon or an NVIDIA GPU, and about three times the
recording's length on a computer's CPU alone (13 minutes for a 4-minute bhajan on 8 cores), one
recording at a time. Each split is kept, and so is each key you shift the
parts to.

**What to expect:** the parts are an AI separation of one finished recording, not separate
microphones, so they are not studio-clean. The harmonium mostly doubles the sung melody, so a
little harmonium stays in the singing and a little singing in the harmonium. Most Sai Rhythms
recordings are 22 kHz mono MP3s at about 95 kbps, which also limits how clean the parts can be. The
singing is the cleanest part; the tabla and harmonium split is the weakest, because no public model
knows those instruments (pop-trained models hear a ringing dayan stroke as a guitar).

## Run it on a server (Docker)

To give a whole group one copy that they open in a browser, run Bhajan Studio on a Linux server
with Docker. On the server:

```bash
git clone https://github.com/PMN123/bhajan-studio.git && cd bhajan-studio
cp .env.example .env        # then put a strong password in it: BHAJAN_PASSWORD=...
docker compose up -d
```

Then open `http://YOUR-SERVER:8777` and sign in with the password. To serve it over HTTPS on your
own domain, set `DOMAIN` in `.env` and run `docker compose --profile https up -d` ([Caddy](https://caddyserver.com)
gets the certificate by itself). There is also an override for NVIDIA GPUs (faster Parts) and a
smaller image without Parts.

[docs/DEPLOY.md](docs/DEPLOY.md) explains it all step by step: what size of server, HTTPS, the
password and allowed host names, backups, updates, and GPUs. Before you share a hosted copy with a
group, read its first section: the songs, recordings and sheet music belong to Sai Rhythms, so ask
the owners first and keep the password on.

## How it works

| Piece | File | Notes |
|---|---|---|
| Launcher | `start.sh`, `start.command` (macOS, Linux), `start.bat`, `start.ps1` (Windows), `server.py` | Checks for uv (offers to install it) and, on macOS, the macOS version; then `uv run` sets up Python 3.12 and the locked dependencies in `.venv` and starts the local server. The server listens on 127.0.0.1 only, answers only requests addressed to 127.0.0.1/localhost that don't come from other websites, picks the next free port if 8777 is busy, and opens your browser when it's ready. |
| Server mode | `server.py`, `Dockerfile`, `docker-compose.yml` | `--host` (or `BHAJAN_HOST`) listens on other addresses. `BHAJAN_PASSWORD` puts everything behind a sign-in page (a signed, HttpOnly cookie for 30 days; HTTP Basic auth for scripts), and `BHAJAN_ALLOWED_HOSTS` limits the host names it answers to. `/api/health` reports the version and whether Parts and OCR are available. The image runs as a non-root user with the cache in a `/data` volume, and waits 10 seconds between requests to Sai Rhythms. |
| Sai Rhythms client | `bhajan/sairhythms.py` | Song index (3,600+ songs), song pages (lyrics, raga scale, reference pitches, range), recordings, and sheet PDFs. Everything is downloaded on demand and cached in `data/`. |
| Sheet reader (OMR) | `bhajan/omr.py`, `bhajan/sheet.py` | Built for the Sai Samarpan engraving. Pitch comes from staff position plus accidentals. Rhythm comes from stems, beams, flags, dots, ties and rests. The printed sargam labels are OCR'd (with macOS Vision, called through pyobjc, or with Tesseract elsewhere) and used to confirm or correct each note. Lyrics are attached from the blue text. A read in which OCR failed is not cached, so it is redone once OCR works. |
| Key detection | `bhajan/audio.py` | Audio is decoded with the ffmpeg build bundled in imageio-ffmpeg. A YIN pitch tracker builds the recording's pitch-class profile, which is matched at all 12 rotations against the sheet's melody (when available) and the raga's scale. |
| Pitch shift | `bhajan/audio.py` | The Rubber Band library through pylibrb, using its R3 ("finer") engine offline with formants preserved. The result is never louder than the original on average (RMS), and a gentle look-ahead limiter keeps peaks under −1 dBFS before MP3 encoding with the bundled ffmpeg. The limiter only acts on loudly mastered recordings (shifting brings back peaks their mastering had limited away), where it costs 1–2 dB of loudness. Shifts by the nearest interval by default, and can also correct the recording's tuning to A440. |
| Parts | `bhajan/stems.py` | Two open separation models run one after the other through [audio-separator](https://github.com/nomadkaraoke/python-audio-separator) on PyTorch (using the GPU on Apple Silicon and NVIDIA's with CUDA, else the CPU): Mel-Band RoFormer splits singing from music, and BS-RoFormer SW's drums, cleaned by harmonic/percussive separation (held notes and drones go back to the harmonium), become the tabla. They run in a separate process, so the server never loads PyTorch and a split can be cancelled. Each stage keeps the exact remainder, so the parts add back up to the recording; they are stored at −6 dB as 24-bit FLAC so none can clip. All the parts are shifted together, as channels of one Rubber Band R3 stretcher with formants kept, so the bleed they share stays in phase and they still add up; the browser mixes the shifted parts live, and downloads get the same peak protection as the whole-recording shift. Session exports put each bhajan's full recording at −18 LUFS (ITU-R BS.1770 meter) and apply that gain to the chosen mix. |
| Synth | `bhajan/synth.py`, `bhajan/instruments.py` | Synthesized flute (the default), recorded piano and harmonium samples (downloaded on first use, retuned, level-matched and looped), tanpura (Sa-Pa or Sa-Ma), and an optional click that follows the sheet's bars. Also writes MIDI with the matching General MIDI instrument. |
| UI | `static/` | Vanilla JS, with [abcjs](https://www.abcjs.net/) vendored for staff notation. A web app manifest and a small service worker (`sw.js`: the page shell is fetched network-first and cached only as an offline fallback; songs, audio and the API are never cached) make it installable as an app. |

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

Run these from the repository folder. `uv run` uses the project's `.venv` (which, on macOS, has
the Vision bindings that the OCR needs).

- `uv run server.py [--port N] [--host H] [--open]` starts the server directly. The `PORT`
  environment variable sets the default port, and `BHAJAN_DATA` moves the cache folder. `--host`
  (or `BHAJAN_HOST`) with anything other than 127.0.0.1 or localhost lets other devices use the
  app; set `BHAJAN_PASSWORD` then, or the server prints a warning. `BHAJAN_ALLOWED_HOSTS` limits
  the host names it answers to. `BHAJAN_SITE_GAP` (seconds, default 0.3) and `BHAJAN_SITE_SLOTS`
  (default 2) pace the requests to Sai Rhythms. See [docs/DEPLOY.md](docs/DEPLOY.md).
- `BHAJAN_OCR=auto|vision|tesseract|none` picks the OCR engine. `auto` (the default) uses Vision on
  macOS, else Tesseract if it is installed, else no OCR; the sheet is then read from the notation
  alone, without the label, lyric and tempo hints.
- `uv run python tools/debug_omr.py page.png out.png` draws the detected notes on a rendered page.
- `uv run python tools/score_omr.py [--ocr vision|tesseract|none] pages/*.png` prints
  bar-consistency and label-agreement metrics.
- `uv run python tools/test_synth.py` checks all three instruments: timing, levels, clicks, key,
  and WAV/MIDI output. If the samples can't be downloaded, the sampled-instrument checks are
  skipped with a message.
- `uv run python tools/test_stems.py` checks the parts without the separation models: a synthetic
  split is read, mixed, shifted, cached and exported as a session, the split queue runs a stand-in
  for the model process, and the loudness meter is checked against the BS.1770 and EBU Tech 3341
  reference values. The API checks use FastAPI's test client, which needs `httpx`; the app doesn't
  install it, so they are skipped with a message unless you run
  `uv run --extra stems --with httpx python tools/test_stems.py`.
- `uv run python tools/test_server.py` starts the real server and checks the health check, the
  sign-in page, its cookie and Basic auth, the allowed host names, the guard against other
  websites, and the file types it serves.
- `uv run python tools/make_icons.py` redraws the app icons in `static/icons/` (only needed if the
  mark changes).
- GitHub Actions runs the three test scripts and starts the app with its launcher on macOS, Linux
  and Windows (`.github/workflows/ci.yml`), and builds and checks the Docker image
  (`.github/workflows/docker.yml`). The image is published to ghcr.io only when the repository
  owner runs that workflow by hand with **publish** ticked.

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
  **[PyObjC](https://pyobjc.readthedocs.io/)** (MIT) connects to Apple's Vision framework for OCR on
  macOS. Elsewhere, OCR uses **[Tesseract](https://github.com/tesseract-ocr/tesseract)** (Apache 2.0)
  if you install it; the Docker image includes it from Debian.
  The rest of the Python stack (FastAPI, Uvicorn, NumPy, SciPy, Pillow) is under permissive licenses.
- **[audio-separator](https://github.com/nomadkaraoke/python-audio-separator)** (MIT) by Nomad
  Karaoke runs the separation models on **[PyTorch](https://pytorch.org/)** (BSD-style). Both are
  installed from PyPI the first time you split a recording and are not included in this repository.
- **Separation models.** audio-separator downloads them on first use, from its own and Ultimate
  Vocal Remover's (UVR) model releases on GitHub, into `data/models/`. They are not included in
  this repository.
  - Singing vs. music: the Mel-Band RoFormer vocal model by Kimberley Jensen, licensed under
    MIT ([source](https://huggingface.co/KimberleyJSN/melbandroformer)).
  - Tabla vs. harmonium: the BS-RoFormer SW model, as published by jarredou. No license is stated
    (its Hugging Face model card says `license: unknown`).
- **Docker image.** Built from `python:3.12-slim-bookworm` (Debian). The optional HTTPS front end
  is the official [Caddy](https://caddyserver.com) image (Apache 2.0), pulled when you use it. The
  image bundles the GPL and AGPL software above (and, with Parts, diffq; see below), so this
  repository doesn't publish one: everyone builds their own. See [docs/DEPLOY.md](docs/DEPLOY.md).
- **App icons.** `static/icons/` holds PNG renderings of "ॐ" in Kohinoor Devanagari Bold (by the
  Indian Type Foundry, shipped with macOS), made by `tools/make_icons.py`. The font itself is not
  included.
- **Sharing the app.** Share this repository (or its ZIP) and let uv download the packages, as the
  launcher does. A ready-made copy that includes `.venv` or the bundled binaries (a zipped folder,
  an `.app`) contains GPL and AGPL software and may only be passed on under those licenses' terms.
  Once a recording has been split, `.venv` also holds
  [diffq](https://github.com/facebookresearch/diffq), a dependency of audio-separator that is
  licensed for non-commercial use only (CC BY-NC 4.0): another reason to share the repository
  rather than a copy with `.venv`.
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
  download; it does not redistribute the recordings, sheet music, lyrics or meanings. Parts split
  from a recording, and the mixes and sessions made from them, are derived from that recording and
  stay in your local cache; sharing them beyond your own use needs the owners' permission (Sai
  Rhythms is run by the Sri Sathya Sai International Organization, SSSIO).
