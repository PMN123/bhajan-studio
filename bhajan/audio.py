"""Recording analysis (which Sa is it sung in?) and high-quality pitch shifting.

Everything runs in-process or with the ffmpeg that imageio-ffmpeg bundles, so
no Homebrew tools are needed: decoding/encoding uses that ffmpeg, and pitch
shifting uses the Rubber Band library (R3 engine) through pylibrb.
"""
from __future__ import annotations

import os
import subprocess
import tempfile

import numpy as np
from scipy import ndimage as ndi

from .media import ffmpeg_exe

PC_NAMES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
SARGAM_PC = {"S": 0, "R1": 1, "R2": 2, "G1": 3, "G2": 4, "M1": 5, "M2": 6, "P": 7,
             "D1": 8, "D2": 9, "N1": 10, "N2": 11}


def load_mono(path: str, sr: int = 22050, max_seconds: float | None = 240) -> np.ndarray:
    cmd = [ffmpeg_exe(), "-nostdin", "-v", "error", "-i", path]
    if max_seconds:
        cmd += ["-t", str(max_seconds)]
    cmd += ["-ac", "1", "-ar", str(sr), "-f", "f32le", "-"]
    raw = subprocess.run(cmd, capture_output=True, check=True).stdout
    return np.frombuffer(raw, np.float32).copy()


def spectral_peaks(y: np.ndarray, sr: int, n_fft=8192, hop=2048, fmin=90.0, fmax=1500.0):
    """Interpolated spectral peaks of every frame -> (midi_float, weight) arrays."""
    if len(y) < n_fft:
        return np.zeros(0), np.zeros(0)
    win = np.hanning(n_fft).astype(np.float32)
    k0 = int(fmin * n_fft / sr)
    k1 = int(fmax * n_fft / sr)
    mids, wts = [], []
    starts = range(0, len(y) - n_fft, hop)
    batch = 64
    starts = list(starts)
    for b in range(0, len(starts), batch):
        idx = starts[b:b + batch]
        frames = np.stack([y[s:s + n_fft] for s in idx]) * win
        mag = np.abs(np.fft.rfft(frames, axis=1))[:, k0 - 1:k1 + 2]
        logm = np.log(mag + 1e-9)
        c = mag[:, 1:-1]
        is_peak = (c > mag[:, :-2]) & (c >= mag[:, 2:])
        frame_max = c.max(axis=1, keepdims=True)
        is_peak &= c > 0.08 * frame_max
        is_peak &= frame_max > 1e-3
        fi, ki = np.nonzero(is_peak)
        if fi.size == 0:
            continue
        a = logm[fi, ki]
        bb = logm[fi, ki + 1]
        cc = logm[fi, ki + 2]
        denom = a - 2 * bb + cc
        delta = np.where(np.abs(denom) > 1e-9, 0.5 * (a - cc) / denom, 0.0)
        freq = (k0 + ki + delta) * sr / n_fft
        mids.append(69 + 12 * np.log2(freq / 440.0))
        wts.append(np.sqrt(c[fi, ki] / frame_max[fi, 0]))
    if not mids:
        return np.zeros(0), np.zeros(0)
    return np.concatenate(mids), np.concatenate(wts)


def track_f0(y: np.ndarray, sr: int, fmin=75.0, fmax=900.0, win=2048, hop=256, thresh=0.18):
    """Vectorised YIN pitch tracker -> (midi_float per voiced frame, clarity weight)."""
    tmin, tmax = int(sr / fmax), int(sr / fmin)
    if len(y) < win + tmax:
        return np.zeros(0), np.zeros(0)
    starts = np.arange(0, len(y) - win - 1, hop)
    out_m, out_w = [], []
    nfft = 1 << int(np.ceil(np.log2(2 * win)))
    for b in range(0, len(starts), 256):
        fr = np.stack([y[s:s + win] for s in starts[b:b + 256]]).astype(np.float64)
        fr -= fr.mean(axis=1, keepdims=True)
        energy = (fr ** 2).mean(axis=1)
        X = np.fft.rfft(fr, nfft, axis=1)
        ac = np.fft.irfft(np.abs(X) ** 2, nfft, axis=1)[:, :tmax + 2]
        cs = np.concatenate([np.zeros((fr.shape[0], 1)), np.cumsum(fr ** 2, axis=1)], axis=1)
        taus = np.arange(tmax + 2)
        e1 = cs[:, win - taus]                     # sum x_j^2, j < win - tau
        e2 = cs[:, win:win + 1] - cs[:, taus]      # sum x_j^2, j >= tau
        d = e1 + e2 - 2 * ac
        d[:, 0] = 0
        cum = np.cumsum(d[:, 1:], axis=1) / np.arange(1, tmax + 2)
        cmnd = np.ones_like(d)
        cmnd[:, 1:] = d[:, 1:] / np.maximum(cum, 1e-12)
        seg = cmnd[:, tmin:tmax + 1]
        below = seg < thresh
        has = below.any(axis=1)
        first = np.where(has, below.argmax(axis=1), seg.argmin(axis=1))
        # walk to the local minimum after the first dip below threshold
        idx = first.copy()
        for _ in range(20):
            nxt = np.minimum(idx + 1, seg.shape[1] - 1)
            better = seg[np.arange(len(idx)), nxt] < seg[np.arange(len(idx)), idx]
            if not better.any():
                break
            idx = np.where(better, nxt, idx)
        val = seg[np.arange(len(idx)), idx]
        t = idx + tmin
        tl = np.clip(t - 1, 1, tmax)
        tr = np.clip(t + 1, 1, tmax + 1)
        a, bb, c = cmnd[np.arange(len(t)), tl], cmnd[np.arange(len(t)), t], cmnd[np.arange(len(t)), tr]
        den = a - 2 * bb + c
        with np.errstate(divide="ignore", invalid="ignore"):  # np.where evaluates both branches
            shift = np.where(np.abs(den) > 1e-12, 0.5 * (a - c) / den, 0.0)
        period = t + np.clip(shift, -1, 1)
        voiced = (val < thresh) & (energy > 1e-5)
        f0 = sr / period
        m = 69 + 12 * np.log2(f0 / 440.0)
        out_m.append(m[voiced])
        out_w.append((1.0 - val[voiced]))
    if not out_m:
        return np.zeros(0), np.zeros(0)
    return np.concatenate(out_m), np.concatenate(out_w)


def _profile(m, w, tuning):
    pc = np.mod(np.round(m - tuning / 100.0), 12).astype(int)
    prof = np.bincount(pc, weights=w, minlength=12)
    return prof / max(prof.sum(), 1e-12)


def _tuning(m, w):
    dev = (m - np.round(m)) * 2 * np.pi
    return float(np.angle(np.sum(w * np.exp(1j * dev))) / (2 * np.pi) * 100)


def raga_template(raga_notes):
    pcs = set()
    for t in raga_notes or []:
        key = t.rstrip("+").upper()
        if key in SARGAM_PC:
            pcs.add(SARGAM_PC[key])
        elif key in ("R", "G", "M", "D", "N"):
            pcs.add({"R": 2, "G": 4, "M": 5, "D": 9, "N": 11}[key])
    if len(pcs) < 4:
        pcs = {0, 2, 4, 5, 7, 9, 11}
    tmpl = np.full(12, -0.3)
    for p in pcs:
        tmpl[p] = 0.45
    tmpl[0] = 1.0
    if 7 in pcs:
        tmpl[7] = 0.75
    elif 5 in pcs:
        tmpl[5] = 0.6
    return tmpl


def _rot_scores(prof, tmpl):
    pz = prof - prof.mean()
    tz = tmpl - tmpl.mean()
    sc = np.array([np.dot(np.roll(pz, -t), tz) for t in range(12)])
    n = np.linalg.norm(pz) * np.linalg.norm(tz)
    return sc / (n + 1e-12)


def analyze_key(path: str, raga_notes: list[str] | None = None, melody_hist: list[float] | None = None,
                priors: list[int] | None = None) -> dict:
    """Estimate the Sa (tonic pitch class) and tuning of a bhajan recording.

    The melody's pitch is tracked frame by frame (YIN). Its pitch-class profile is
    compared, at all 12 rotations, with (a) the note distribution of the sheet
    music when we have it and (b) a template built from the raga's notes.
    """
    sr = 16000
    y = load_mono(path, sr)
    m, w = track_f0(y, sr)
    if m.size < 50:
        return {"ok": False, "error": "no melody found in the recording"}
    tuning = _tuning(m, w)
    prof = _profile(m, w, tuning)
    s_raga = _rot_scores(prof, raga_template(raga_notes))
    if melody_hist is not None and np.sum(melody_hist) > 0:
        s_mel = _rot_scores(prof, np.asarray(melody_hist, float))
        scores = 0.7 * s_mel + 0.3 * s_raga
        basis = "sheet melody + raga"
    else:
        scores = s_raga
        basis = "raga scale"
    for p in priors or []:
        if p is not None:
            scores[p % 12] += 0.02
    order = np.argsort(-scores)
    best = int(order[0])
    margin = float(scores[order[0]] - scores[order[1]])
    # typical sung register: median voiced pitch relative to Sa
    rel = m - (best + tuning / 100.0)
    return {
        "ok": True,
        "sa_pc": best,
        "sa_name": PC_NAMES[best],
        "tuning_cents": round(tuning, 1),
        "basis": basis,
        "confidence": "high" if margin > 0.15 else "medium" if margin > 0.06 else "low",
        "margin": round(margin, 3),
        "candidates": [{"pc": int(t), "name": PC_NAMES[t], "score": round(float(scores[t]), 3)} for t in order[:4]],
        "profile": [round(float(v), 4) for v in prof],
        "median_midi": round(float(np.median(m)), 1),
        "voiced_seconds": round(len(m) * 256 / sr, 1),
        "seconds_analyzed": round(len(y) / sr, 1),
    }


def semitone_shift(from_pc: int, to_pc: int, octave: int = 0) -> int:
    """Smallest move from one Sa to another (-6..+5), plus whole octaves if asked."""
    d = (to_pc - from_pc) % 12
    if d > 6:
        d -= 12
    return d + 12 * octave


# ----------------------------------------------------------------- pitch shift
SHIFT_SR = 44100
_BLOCK = 65536               # frames per ffmpeg pipe read/write
_RB_BLOCK = 1024             # frames per Rubber Band call: with large offline blocks the
                             # output drifts late (~600 samples at 65536); at 1024 it stays
                             # within ~0.6 ms of the input, like the rubberband CLI
_CEILING = 10 ** (-1 / 20)   # -1 dBFS sample peak: headroom for MP3 encoding overshoot
_ENV_HOP = 32                # limiter envelope resolution, frames


def _decoded_blocks(src: str, sr: int = SHIFT_SR, channels: int = 2):
    """Stream-decode with the bundled ffmpeg -> ((channels, n) float32 block, is_last)."""
    frame = 4 * channels
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen([ffmpeg_exe(), "-nostdin", "-v", "error", "-i", src, "-vn",
                                 "-ac", str(channels), "-ar", str(sr), "-f", "f32le", "pipe:1"],
                                stdout=subprocess.PIPE, stderr=err)
        try:
            buf = proc.stdout.read(_BLOCK * frame)
            while buf:
                nxt = proc.stdout.read(_BLOCK * frame)
                n = len(buf) // frame
                blk = np.frombuffer(buf, np.float32, n * channels).reshape(n, channels).T
                yield np.ascontiguousarray(blk), not nxt
                buf = nxt
        finally:
            proc.stdout.close()
            rc = proc.wait()
        if rc:
            err.seek(0)
            msg = err.read().decode(errors="replace").strip()[-400:]
            raise RuntimeError(f"ffmpeg could not decode {os.path.basename(src)}: {msg}")


def _release(x: np.ndarray, coef: float) -> np.ndarray:
    """r[i] = max(x[i], coef * r[i-1]) for x >= 0 (instant attack, exponential release),
    vectorised as a running maximum in the log domain."""
    k = np.arange(len(x)) * np.log(coef)
    with np.errstate(divide="ignore"):
        lx = np.log(x)
    return np.exp(np.maximum.accumulate(lx - k) + k)


def _clip_guard(y: np.ndarray, ref_rms: float, sr: int = SHIFT_SR, ceiling: float = _CEILING,
                attack: float = 0.005, release: float = 0.2):
    """Gain curve keeping shifted audio no louder than the source and under `ceiling`.

    Why it is needed: some Sai Rhythms mp3s are brick-wall-limited masters (peaks
    pinned at full scale, crest factor ~3.2).  A phase vocoder rebuilds every
    partial with new phases, which undoes the limiter's peak alignment: the RMS
    rises only ~3 %, but the crest factor returns to ~4 and peaks reach ~2x full
    scale all through the track (not isolated clicks).  One overall gain that
    matched the input peak would cost ~6 dB of loudness (the rubberband CLI turns
    the whole track down ~2.5 dB and still clips), so instead: (1) scale down
    only if the output came out louder (RMS) than the source, then (2) a
    linked-stereo look-ahead peak limiter (5 ms attack, 200 ms release) turns
    down just the moments that would exceed `ceiling`.  On quieter recordings it
    does nothing.  y is (frames, channels); returns (frame positions, gains) for
    np.interp.
    """
    n = len(y)
    hop = _ENV_HOP
    nb = -(-n // hop)
    peak = np.zeros(nb, np.float64)
    sumsq = 0.0
    step = hop * 8192
    for s in range(0, n, step):
        c = y[s:s + step]
        sumsq += float(np.einsum("ij,ij->", c, c, dtype=np.float64))
        a = np.abs(c).max(axis=1)
        a = np.pad(a, (0, -len(a) % hop))
        peak[s // hop:s // hop + len(a) // hop] = a.reshape(-1, hop).max(axis=1)
    rms = np.sqrt(sumsq / max(y.size, 1))
    g0 = min(1.0, ref_rms / rms) if rms > 0 else 1.0
    with np.errstate(divide="ignore"):
        need = np.maximum(0.0, 20 * np.log10(peak * g0 / ceiling))   # dB of reduction needed
    a = int(np.ceil(attack * sr / hop))
    # spread each requirement over +-(a+1) blocks so that the smoothing below, and the
    # linear interpolation between neighbouring block gains, never undershoot it
    red = ndi.maximum_filter1d(need, size=2 * a + 3, mode="constant")
    red = _release(red, float(np.exp(-hop / (release * sr))))
    red = ndi.uniform_filter1d(red, size=2 * a + 1, mode="nearest")
    centers = np.arange(nb) * hop + (hop - 1) / 2
    return centers, g0 * 10 ** (-red / 20)


def _encode_mp3(y: np.ndarray, centers: np.ndarray, gains: np.ndarray, path: str,
                sr: int = SHIFT_SR) -> None:
    """Apply the gain curve and encode (frames, 2) float32 audio to MP3 (LAME VBR q2)."""
    with tempfile.TemporaryFile() as err:
        proc = subprocess.Popen([ffmpeg_exe(), "-v", "error", "-y", "-f", "f32le", "-ar", str(sr),
                                 "-ac", str(y.shape[1]), "-i", "pipe:0", "-codec:a", "libmp3lame",
                                 "-q:a", "2", "-f", "mp3", path], stdin=subprocess.PIPE, stderr=err)
        try:
            for s in range(0, len(y), _BLOCK * 4):
                c = y[s:s + _BLOCK * 4]
                g = np.interp(np.arange(s, s + len(c)), centers, gains).astype(np.float32)
                proc.stdin.write((c * g[:, None]).tobytes())
        except BrokenPipeError:
            pass                                    # ffmpeg died; reported below
        finally:
            try:
                proc.stdin.close()
            except BrokenPipeError:
                pass
            rc = proc.wait()
        if rc:
            err.seek(0)
            raise RuntimeError("ffmpeg could not encode the MP3: "
                               + err.read().decode(errors="replace").strip()[-400:])


def _pieces(blk: np.ndarray, last: bool):
    """Split a decoded block into _RB_BLOCK pieces -> (piece, is_final_piece)."""
    n = blk.shape[1]
    for i in range(0, n, _RB_BLOCK):
        yield np.ascontiguousarray(blk[:, i:i + _RB_BLOCK]), last and i + _RB_BLOCK >= n


def _drain(st, out: np.ndarray, pos: int) -> int:
    while (avail := st.available()) > 0:
        r = st.retrieve(avail)                      # (channels, n)
        n = min(r.shape[1], len(out) - pos)
        out[pos:pos + n] = r[:, :n].T
        pos += n
    return pos


def pitch_shift(src: str, dst: str, semitones: float, formant: bool = True) -> str:
    """Pitch-shift an audio file with Rubber Band's R3 engine (tempo unchanged) -> MP3.

    Rubber Band runs in offline mode (a study pass over the whole file, then the
    processing pass), fed by two streaming ffmpeg decodes so the input is never
    held in memory; only the output is (~21 MB per minute) for clip protection.
    """
    st = None
    if abs(semitones) >= 1e-6:
        import pylibrb                              # loads the native library; only needed here
        O = pylibrb.Option
        opts = (O.PROCESS_OFFLINE | O.ENGINE_FINER | O.CHANNELS_TOGETHER
                | (O.FORMANT_PRESERVED if formant else O.FORMANT_SHIFTED))
        st = pylibrb.RubberBandStretcher(SHIFT_SR, 2, opts, 1.0, 2.0 ** (semitones / 12.0))
        st.set_max_process_size(_RB_BLOCK)
    # pass 1: length and loudness of the source; Rubber Band studies the whole file
    n_in, sumsq = 0, 0.0
    for blk, last in _decoded_blocks(src):
        n_in += blk.shape[1]
        sumsq += float(np.einsum("ij,ij->", blk, blk, dtype=np.float64))
        if st is not None:
            for piece, final in _pieces(blk, last):
                st.study(piece, final)
    if n_in == 0:
        raise RuntimeError(f"no audio could be decoded from {os.path.basename(src)}")
    # pass 2: shift
    out = np.empty((n_in, 2), np.float32)
    pos = 0
    if st is not None:
        st.set_expected_input_duration(n_in)
    for blk, last in _decoded_blocks(src):
        if st is None:
            n = min(blk.shape[1], n_in - pos)
            out[pos:pos + n] = blk[:, :n].T
            pos += n
        else:
            for piece, final in _pieces(blk, last):
                st.process(piece, final)
                pos = _drain(st, out, pos)
    if st is not None:
        pos = _drain(st, out, pos)
    out = out[:pos]
    centers, gains = _clip_guard(out, np.sqrt(sumsq / (2 * n_in)))
    tmp = dst + ".part.mp3"
    try:
        _encode_mp3(out, centers, gains, tmp)
        os.replace(tmp, dst)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    return dst
