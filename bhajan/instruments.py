"""Sampled piano and harmonium: real recorded notes instead of wavetables.

The samples come from the pre-rendered General MIDI soundfonts of the MIDI.js project
(https://github.com/gleitz/midi-js-soundfonts): one MP3 per semitone (A0..C8), about 3 s each.

* Harmonium = "reed_organ" from the FluidR3_GM soundfont (Frank Wen, CC BY 3.0). Of the reed organ and
  accordion samples in the three soundfonts it was closest to the harmonium heard in Sai Rhythms
  instrumental tracks, is tuned within 5 cents and is perfectly even from note to note.
* Piano = "acoustic_grand_piano" from the FatBoy soundfont (https://fatboy.site, CC BY-SA 3.0): the
  cleanest and most even of the three pianos across the melody range.

Each instrument is downloaded once on first use (about 3 MB, checked against a pinned SHA-256), cached under
<data>/instruments/, and every note that a render needs is decoded with the bundled ffmpeg straight to the render
sample rate. Pitch is corrected during decoding (by decoding at a slightly different rate) when a sample is more
than 3 cents off, loudness is balanced across notes, and notes longer than the sample loop seamlessly
(sustained harmonium) or keep decaying (piano).
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

from . import media

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass(frozen=True)
class Source:
    soundfont: str
    name: str
    sha256: str
    sustained: bool      # True: loop the steady part for long notes; False: let it decay
    release: float       # seconds of release after the note ends (key/damper up)
    gap: float           # seconds the key is lifted before the next note (articulation)
    gm_program: int      # General MIDI program (0-based)
    credit: str
    tone: tuple = ()     # (high-shelf dB, shelf Hz, 1st-order low-pass Hz) applied to the mix of notes
    limit_db: float = 0.0  # gentle peak limiting of the mix (hammer attacks), so it is not much quieter than the flute
    # Repeated note (same key struck again): () = finish the release before the new note (a key-lift gap, as on the
    # harmonium); (lead s, fade s) = the damper touches the string only `lead` before the hammer strikes again and the
    # old vibration dies away over `fade`, under the new attack (a piano string is still ringing when it is re-struck)
    restrike: tuple = ()


SOURCES = {
    "harmonium": Source("FluidR3_GM", "reed_organ",
                        "c614da40c1d04687c1b6f2ed132bcd0a17c26f79cade80408e182b07b8602e08",
                        sustained=True, release=0.085, gap=0.010, gm_program=20,
                        credit="FluidR3_GM soundfont by Frank Wen, CC BY 3.0",
                        # the reed organ is brighter above ~1.5 kHz than the harmonium in Sai Rhythms recordings
                        tone=(-4.0, 1800.0, 4500.0)),
    "piano": Source("FatBoy", "acoustic_grand_piano",
                    "dd9054bbeaff45ab3ce44164ffd08d227fb2ba63dc5530d54644b8a1db7dba3a",
                    sustained=False, release=0.13, gap=0.0, gm_program=0,
                    credit="FatBoy soundfont (https://fatboy.site), CC BY-SA 3.0", limit_db=6.0,
                    # repeated notes are re-articulated (a ~9 dB dip, like the flute's) instead of chopped by
                    # ~12 ms of silence (a ~18 dB hole), which made the piano sound choppy on repeated swaras
                    restrike=(0.015, 0.070)),
}
SOUND_VERSION = 2           # bump whenever the rendered piano/harmonium sound changes (part of synth.cache_tag)
URLS = ("https://gleitz.github.io/midi-js-soundfonts/{sf}/{name}-mp3.js",
        "https://cdn.jsdelivr.net/gh/gleitz/midi-js-soundfonts@gh-pages/{sf}/{name}-mp3.js")
LO, HI = 21, 108            # MIDI range of the soundfonts (A0..C8); other notes are shifted from the nearest
ANALYSIS_SR = 32000
ANALYSIS_VERSION = 1
MAX_DOWNLOAD = 12 * 1024 * 1024
RETRY_AFTER = 120.0         # seconds before retrying a failed download
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) BhajanStudio/1.0"
_PC = {"C": 0, "Db": 1, "D": 2, "Eb": 3, "E": 4, "F": 5, "Gb": 6, "G": 7, "Ab": 8, "A": 9, "Bb": 10, "B": 11}
_NOTE_RE = re.compile(rb'"([A-G]b?)(-?\d)"\s*:\s*"data:audio/mp3;base64,([A-Za-z0-9+/=]+)"')


class Unavailable(RuntimeError):
    """The samples could not be downloaded or decoded (the caller falls back to a synthesized sound)."""


def data_dir() -> str:
    base = os.environ.get("BHAJAN_DATA", os.path.join(ROOT, "data"))
    return os.path.join(base, "instruments")


def _paths(src: Source) -> tuple[str, str]:
    stem = os.path.join(data_dir(), f"{src.soundfont}-{src.name}")
    return stem + ".npz", stem + ".json"


def is_cached(instrument: str) -> bool:
    src = SOURCES.get(instrument)
    return bool(src) and all(os.path.exists(p) for p in _paths(src))


def gm_program(instrument: str, default: int = 20) -> int:
    src = SOURCES.get(instrument)
    return src.gm_program if src else default


# ------------------------------------------------------------------ download + cache
_locks = {k: threading.Lock() for k in SOURCES}
_store: dict[str, dict] = {}          # instrument -> {"mp3": {midi: bytes}, "meta": {...}}
_failed: dict[str, tuple[float, str]] = {}


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # pragma: no cover
        return ssl.create_default_context()


def _download(src: Source) -> bytes:
    errors = []
    for tmpl in URLS:
        url = tmpl.format(sf=src.soundfont, name=src.name)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=15, context=_ssl_context()) as r:
                data = r.read(MAX_DOWNLOAD + 1)
            if len(data) > MAX_DOWNLOAD:
                raise ValueError("file is unexpectedly large")
            digest = hashlib.sha256(data).hexdigest()
            if digest != src.sha256:
                raise ValueError(f"checksum mismatch ({digest[:12]}...)")
            return data
        except Exception as e:  # try the next mirror
            errors.append(f"{url}: {e}")
    raise Unavailable("could not download the samples: " + "; ".join(errors))


def _parse(data: bytes) -> dict[int, bytes]:
    out = {}
    for name, octave, b64 in _NOTE_RE.findall(data):
        out[12 * (int(octave) + 1) + _PC[name.decode()]] = base64.b64decode(b64)
    if len(out) < 80:
        raise Unavailable(f"unexpected soundfont format ({len(out)} notes)")
    return out


def _atomic_write(path: str, write) -> None:
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.part"
    try:
        with open(tmp, "wb") as f:
            write(f)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _ffmpeg() -> str:
    try:
        return media.ffmpeg_exe()
    except Exception as e:
        raise Unavailable(f"ffmpeg is not available ({e})") from e


def _decode(mp3: bytes, rate: int) -> np.ndarray:
    cmd = [_ffmpeg(), "-nostdin", "-v", "error", "-f", "mp3", "-i", "pipe:0",
           "-ac", "1", "-ar", str(int(rate)), "-f", "f32le", "pipe:1"]
    p = subprocess.run(cmd, input=mp3, capture_output=True, timeout=60)
    if p.returncode != 0 or not p.stdout:
        raise Unavailable("could not decode a sample: " + p.stderr.decode("utf-8", "replace")[-300:])
    return np.frombuffer(p.stdout, np.float32).copy()


def _nominal(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def _onset(x: np.ndarray) -> int:
    peak = float(np.abs(x).max()) if len(x) else 0.0
    if peak <= 0:
        return 0
    i = int(np.argmax(np.abs(x) > peak * 10 ** (-50 / 20)))
    return max(0, i - int(0.001 * ANALYSIS_SR))


def _h1_cents(x: np.ndarray, m: int, sr: int) -> float:
    """Deviation of the fundamental from equal temperament (A=440), in cents."""
    seg = x[int(0.15 * sr): int(1.15 * sr)].astype(np.float64)
    if len(seg) < sr // 2 or not np.any(seg):
        return 0.0
    n = 1 << 18
    spec = np.abs(np.fft.rfft(seg * np.hanning(len(seg)), n))
    df = sr / n
    f = _nominal(m)
    lo, hi = int(f * 0.95 / df), int(f * 1.05 / df) + 1
    k = lo + int(np.argmax(spec[lo:hi]))
    a, b, c = np.log(spec[k - 1:k + 2] + 1e-12)
    den = a - 2 * b + c
    k = k + (0.5 * (a - c) / den if abs(den) > 1e-12 else 0.0)
    cents = 1200 * np.log2(k * df / f)
    return float(cents) if abs(cents) < 40 else 0.0


def _analyse(src: Source, mp3: dict[int, bytes]) -> dict:
    """Per-note onset, tuning and loudness-balancing gain (computed once, saved as JSON)."""
    sr = ANALYSIS_SR
    midis = sorted(m for m in mp3 if LO <= m <= HI)
    with ThreadPoolExecutor(8) as ex:
        waves = dict(zip(midis, ex.map(lambda m: _decode(mp3[m], sr), midis)))
    notes, level = {}, {}
    for m in midis:
        x = waves[m]
        on = _onset(x)
        x = x[on:]
        a, b = (0.2, 1.0) if src.sustained else (0.0, 0.3)
        seg = x[int(a * sr): int(b * sr)].astype(np.float64)
        level[m] = 10 * np.log10(np.mean(seg ** 2) + 1e-15)
        notes[m] = {"onset": round(on / sr, 5), "cents": round(_h1_cents(x, m, sr), 2)}
    # smooth loudness curve over the keyboard; each note gets the gain that puts it on the curve
    ms = np.array(midis, float)
    lv = np.array([level[m] for m in midis])
    fit = np.polyval(np.polyfit(ms, lv, 3), ms)
    for m, g in zip(midis, np.clip(fit - lv, -9, 9)):
        notes[m]["gain_db"] = round(float(g), 2)
    return {"version": ANALYSIS_VERSION, "sha256": src.sha256, "soundfont": src.soundfont,
            "instrument": src.name, "credit": src.credit, "notes": {str(m): v for m, v in notes.items()}}


def ensure(instrument: str) -> dict:
    """Download (first use only) and load an instrument's samples. Raises Unavailable."""
    if instrument in _store:
        return _store[instrument]
    src = SOURCES[instrument]
    with _locks[instrument]:
        if instrument in _store:
            return _store[instrument]
        t_fail = _failed.get(instrument)
        if t_fail and time.time() - t_fail[0] < RETRY_AFTER:
            raise Unavailable(t_fail[1])
        try:
            npz, meta_path = _paths(src)
            os.makedirs(os.path.dirname(npz), exist_ok=True)
            mp3 = None
            if os.path.exists(npz):
                try:
                    with np.load(npz) as z:
                        mp3 = {int(k[1:]): z[k].tobytes() for k in z.files if k.startswith("m")}
                except Exception:
                    mp3 = None  # damaged cache: fetch again
            if not mp3:
                mp3 = _parse(_download(src))
                _atomic_write(npz, lambda f: np.savez(f, **{f"m{m}": np.frombuffer(b, np.uint8) for m, b in mp3.items()}))
            meta = None
            if os.path.exists(meta_path):
                try:
                    with open(meta_path, encoding="utf-8") as f:
                        meta = json.load(f)
                    if meta.get("version") != ANALYSIS_VERSION or meta.get("sha256") != src.sha256:
                        meta = None
                except Exception:
                    meta = None
            if meta is None:
                meta = _analyse(src, mp3)
                _atomic_write(meta_path, lambda f: f.write(json.dumps(meta, indent=1).encode()))
        except Unavailable as e:
            _failed[instrument] = (time.time(), str(e))
            raise
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            _failed[instrument] = (time.time(), msg)
            raise Unavailable(msg) from e
        _failed.pop(instrument, None)
        _store[instrument] = {"mp3": mp3, "meta": meta}
        return _store[instrument]


# ------------------------------------------------------------------ voices (one prepared note)
class Voice:
    """One note at one sample rate: the sample plus a baked crossfade loop for notes longer than it."""

    __slots__ = ("x", "le", "head", "body", "rate")

    def __init__(self, x: np.ndarray, ls: int, le: int, xf: int, rate_db: float):
        self.x, self.le, self.rate = x, le, rate_db           # rate_db: dB per sample (0 = steady)
        L = le - ls
        gain_l = 10 ** (rate_db * L / 20)                      # level drop across the loop
        w = np.linspace(0.0, 1.0, xf, dtype=np.float32)
        blend = (x[le - xf:le] * (1 - w) + x[ls - xf:ls] * (np.float32(gain_l) * w)).astype(np.float32)
        self.head = np.concatenate([x[:le - xf], blend])
        body = np.concatenate([x[ls:le - xf], blend])
        if rate_db:
            body = body * (10 ** (-rate_db * np.arange(L) / 20)).astype(np.float32)  # flatten the decay
        self.body = body.astype(np.float32)

    def render(self, n: int) -> np.ndarray:
        if n <= self.le:
            return self.x[:n]
        k = n - self.le
        L = len(self.body)
        ext = np.tile(self.body, -(-k // L))[:k]
        if self.rate:
            ext = ext * (10 ** (self.rate * (L + np.arange(k)) / 20)).astype(np.float32)
        return np.concatenate([self.head, ext])


def _loop_end(x: np.ndarray, ls: int, le: int, xf: int, period: float) -> int:
    """Loop end near `le` whose preceding waveform best matches the one before the loop start."""
    ref = x[ls - xf:ls].astype(np.float64)
    span = int(2 * period) + 2
    lo = max(ls + 4 * xf, le - span)
    if lo >= le:
        return le
    win = np.lib.stride_tricks.sliding_window_view(x[lo - xf:le].astype(np.float64), xf)
    num = win @ ref
    den = np.sqrt((win ** 2).sum(axis=1) * (ref ** 2).sum()) + 1e-12
    return lo + int(np.argmax(num / den))


def _prepare(instrument: str, midi: int, sr: int) -> Voice:
    store = ensure(instrument)
    src = SOURCES[instrument]
    m_src = int(min(HI, max(LO, midi)))
    info = store["meta"]["notes"].get(str(m_src)) or {"onset": 0.0, "cents": 0.0, "gain_db": 0.0}
    shift = 100.0 * (midi - m_src) - (info["cents"] if abs(info["cents"]) > 3 else 0.0)
    rate = int(round(sr * 2 ** (-shift / 1200)))             # decode at this rate, play at sr
    x = _decode(store["mp3"][m_src], rate)
    x = x[int(round(info["onset"] * rate)):]
    fade = min(len(x), int(0.0015 * sr))
    x[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
    x *= np.float32(10 ** (info["gain_db"] / 20))
    period = sr / _nominal(midi)
    xf = int(0.045 * sr)
    if src.sustained:
        ls = int(0.6 * sr)
        le = _loop_end(x, ls, len(x) - int(0.1 * sr), xf, period)
        rate_db = 0.0
    else:
        le_t = len(x) - int(0.05 * sr)
        ls = le_t - int(0.6 * sr)
        le = _loop_end(x, ls, le_t, xf, period)
        hop = int(0.01 * sr)
        seg = x[ls:le]
        nfr = len(seg) // hop
        db = 10 * np.log10((seg[: nfr * hop].astype(np.float64).reshape(nfr, hop) ** 2).mean(axis=1) + 1e-15)
        slope = np.polyfit(np.arange(nfr) * hop / sr, db, 1)[0] if nfr > 4 else -10.0
        rate_db = float(np.clip(slope, -40.0, -3.0)) / sr      # keep decaying after the sample ends
    return Voice(x.astype(np.float32), ls, le, xf, rate_db)


_voices: "OrderedDict[tuple, Voice]" = OrderedDict()
_voices_lock = threading.Lock()
_MAX_VOICES = 256


def voices(instrument: str, midis, sr: int) -> dict[int, Voice]:
    want = sorted({int(m) for m in midis})
    out, missing = {}, []
    with _voices_lock:
        for m in want:
            v = _voices.get((instrument, m, sr))
            if v is None:
                missing.append(m)
            else:
                _voices.move_to_end((instrument, m, sr))
                out[m] = v
    if missing:
        ensure(instrument)  # download once, before the parallel decode
        with ThreadPoolExecutor(min(8, len(missing))) as ex:
            made = list(ex.map(lambda m: _prepare(instrument, m, sr), missing))
        with _voices_lock:
            for m, v in zip(missing, made):
                _voices[(instrument, m, sr)] = v
                out[m] = v
            while len(_voices) > _MAX_VOICES:
                _voices.popitem(last=False)
    return out


# ------------------------------------------------------------------ rendering
def _tone(x: np.ndarray, sr: int, shelf_db: float, shelf_hz: float, lowpass_hz: float) -> np.ndarray:
    from scipy import signal
    y = x.astype(np.float64)
    if shelf_db and shelf_hz < 0.45 * sr:  # RBJ cookbook high shelf, slope 1
        A, w0 = 10 ** (shelf_db / 40), 2 * np.pi * shelf_hz / sr
        al, c, sa = np.sin(w0) / np.sqrt(2), np.cos(w0), np.sqrt(10 ** (shelf_db / 40))
        b = [A * ((A + 1) + (A - 1) * c + 2 * sa * al), -2 * A * ((A - 1) + (A + 1) * c),
             A * ((A + 1) + (A - 1) * c - 2 * sa * al)]
        a = [(A + 1) - (A - 1) * c + 2 * sa * al, 2 * ((A - 1) - (A + 1) * c), (A + 1) - (A - 1) * c - 2 * sa * al]
        y = signal.lfilter(np.array(b) / a[0], np.array(a) / a[0], y)
    if lowpass_hz and lowpass_hz < 0.45 * sr:
        b, a = signal.butter(1, lowpass_hz, fs=sr)
        y = signal.lfilter(b, a, y)
    return y.astype(np.float32)


def _limit(x: np.ndarray, sr: int, reduction_db: float) -> np.ndarray:
    """Look-ahead peak limiter: peaks come down by `reduction_db`, gain recovers over ~60 ms."""
    from scipy import ndimage, signal
    a = np.abs(x.astype(np.float64))
    peak = a.max() if len(a) else 0.0
    if peak <= 0:
        return x
    thr = peak * 10 ** (-reduction_db / 20)
    w = 2 * int(0.004 * sr) + 1
    g = np.minimum(1.0, thr / np.maximum(ndimage.maximum_filter1d(a, w), 1e-12))
    g = ndimage.uniform_filter1d(ndimage.minimum_filter1d(g, w), w)   # never above the gain each peak needs
    k = 1 - np.exp(-1 / (0.06 * sr))
    g = np.minimum(g, signal.lfilter([k], [1, k - 1], g, zi=[g[0] * (1 - k)])[0])  # slow recovery
    return (x * g).astype(np.float32)


def _release_curve(n: int) -> np.ndarray:
    t = np.arange(n, dtype=np.float32) / max(1, n)
    return np.exp(-5.0 * t) * (0.5 + 0.5 * np.cos(np.pi * t))


def render_notes(instrument: str, notes, spq: float, total: int, offset: int, sr: int) -> np.ndarray:
    """Sum the sampled notes [(start_q, dur_q, midi)] into a mono buffer of `total` samples."""
    src = SOURCES[instrument]
    out = np.zeros(total, np.float32)
    if not notes:
        return out
    vs = voices(instrument, [int(round(m)) for _, _, m in notes], sr)
    rel = max(1, int(src.release * sr))
    gap = int(src.gap * sr)
    starts = [offset + int(round(s * spq * sr)) for s, _, _ in notes]
    next_same: dict[int, int] = {}
    nxt = [None] * len(notes)
    for i in range(len(notes) - 1, -1, -1):          # next onset of the same pitch (re-struck key)
        m = int(round(notes[i][2]))
        nxt[i] = next_same.get(m)
        next_same[m] = starts[i]
    for i, (start_q, dur_q, m) in enumerate(notes):
        s0 = starts[i]
        if s0 >= total:
            continue
        n_on = max(1, int(round(dur_q * spq * sr)) - gap)
        r = rel
        if nxt[i] is not None and src.restrike:       # piano: the string is still ringing when it is struck again
            room = nxt[i] - s0
            lead, fade = int(src.restrike[0] * sr), max(1, int(src.restrike[1] * sr))
            if room - n_on <= lead:                    # repeated note: the damper only touches the string briefly
                n_on = max(1, min(n_on, room - lead))
                r = fade                               # and the old vibration dies away under the new attack
            else:                                      # a rest or other notes in between: the usual release,
                r = max(1, min(rel, room - n_on - lead + fade))  # mostly over before the key is struck again
        elif nxt[i] is not None:                      # finish the release before the key is struck again
            room = nxt[i] - s0
            r = max(1, min(rel, room - n_on)) if room - n_on >= int(0.02 * sr) else int(0.02 * sr)
            n_on = max(1, min(n_on, room - r))
        n = min(n_on + r, total - s0)
        sig = vs[int(round(m))].render(n).copy()
        if n > n_on:
            sig[n_on:] *= _release_curve(r)[: n - n_on]
        out[s0:s0 + n] += sig
    if src.tone:
        out = _tone(out, sr, *src.tone)
    if src.limit_db:
        out = _limit(out, sr, src.limit_db)
    if src.sustained:  # gentle bellows: the level of a hand-pumped reed instrument breathes a little
        tt = np.arange(total, dtype=np.float32) / sr
        out *= 1 + 0.035 * np.sin(2 * np.pi * 0.21 * tt) + 0.015 * np.sin(2 * np.pi * 0.53 * tt + 1.0)
    return out


def warn(msg: str) -> None:
    print(f"[bhajan.instruments] {msg}", file=sys.stderr, flush=True)
