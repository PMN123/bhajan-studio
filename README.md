# Bhajan Studio

Pick any bhajan from [Sai Rhythms](https://sairhythms.org), choose your Sa (key), and get:

1. **The original recording, shifted to your key.** Rubber Band's R3 engine with formant
   preservation, so the singer still sounds like themselves. The app first works out which key the
   recording is actually sung in, which is often not the published reference pitch.
2. **The Sai Samarpan sheet music, read note by note, transposed and played back** on a
   synthesized harmonium or flute (instrument only by default; a soft tanpura drone is optional). You can view it as sargam, as transposed
   staff notation, or as the original sheet with every detected note outlined. WAV and MIDI
   downloads are included.

## Run

```bash
uv run server.py
```

Then open <http://127.0.0.1:8777>. The first run installs the Python dependencies into `.venv`.

Requirements (macOS): `ffmpeg` and `rubberband` (`brew install ffmpeg rubberband`), plus Xcode's
command-line tools. The Swift OCR helper in `tools/` is compiled automatically on first use.
`tesseract` is used as a fallback if Vision OCR isn't available.

All downloads and results are cached in `data/`. Delete that folder to start fresh.

## How it works

| Piece | File | Notes |
|---|---|---|
| Sai Rhythms client | `bhajan/sairhythms.py` | Song index (3,600+ songs), song pages (lyrics, raga scale, reference pitches, range), recordings, and sheet PDFs |
| Sheet reader (OMR) | `bhajan/omr.py`, `bhajan/sheet.py` | Built for the Sai Samarpan engraving. Pitch comes from staff position plus accidentals. Rhythm comes from stems, beams, flags, dots, ties and rests. The printed sargam labels are OCR'd with macOS Vision and used to confirm or correct each note. Lyrics are attached from the blue text. |
| Key detection | `bhajan/audio.py` | A YIN pitch tracker builds the recording's pitch-class profile, which is matched at all 12 rotations against the sheet's melody (when available) and the raga's scale. |
| Pitch shift | `bhajan/audio.py` | `rubberband -3 -F --centre-focus` (R3 engine, formants preserved). Shifts by the nearest interval by default, and can also correct the recording's tuning to A440. |
| Synth | `bhajan/synth.py` | Additive harmonium/flute, tanpura (Sa-Pa or Sa-Ma), optional click. Also writes MIDI. |
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
so the piece plays straight through at one tempo. Key detection is very reliable when the song has sheet music; it relies on the
raga scale alone otherwise.

### Developer tools

- `python3 -I tools/debug_omr.py page.png out.png` draws the detected notes on a rendered page.
- `python3 -I tools/score_omr.py pages/*.png` prints bar-consistency and label-agreement metrics.
