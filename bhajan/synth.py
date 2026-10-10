"""Render a bhajan melody as audio: harmonium or flute over a tanpura drone.

Wavetable synthesis keeps it fast (a few seconds for a whole song) and band-limited.
"""
from __future__ import annotations

import struct
import wave

import numpy as np

TABLE = 4096
_rng = np.random.default_rng(7)
_PHASES = _rng.uniform(0, 2 * np.pi, 64)


def _freq(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def _table(amps: np.ndarray) -> np.ndarray:
    """One period of a waveform with the given harmonic amplitudes (index 0 = fundamental)."""
    spec = np.zeros(TABLE // 2 + 1, complex)
    k = np.arange(1, len(amps) + 1)
    spec[k] = amps * np.exp(1j * _PHASES[: len(amps)]) * (TABLE / 2)
    return np.fft.irfft(spec, TABLE).astype(np.float32)


def _osc(table: np.ndarray, freq, n: int, sr: int, phase0=0.0) -> np.ndarray:
    """Table-lookup oscillator; freq may be a scalar or a per-sample array."""
    if np.isscalar(freq):
        ph = phase0 + np.arange(n, dtype=np.float64) * (freq / sr)
    else:
        ph = phase0 + np.cumsum(np.asarray(freq, np.float64) / sr)
    pos = (ph % 1.0) * TABLE
    i = pos.astype(np.int64)
    frac = (pos - i).astype(np.float32)
    return table[i % TABLE] * (1 - frac) + table[(i + 1) % TABLE] * frac


def _harmonium_amps(f: float, sr: int) -> np.ndarray:
    n = max(1, min(40, int(0.45 * sr / f)))
    k = np.arange(1, n + 1)
    hz = k * f
    amps = k ** -0.75 * (1 + 1.6 * np.exp(-((hz - 1300) / 800) ** 2)) * np.where(k % 2 == 0, 0.8, 1.0)
    amps *= np.clip((0.45 * sr - hz) / (0.1 * sr), 0, 1)  # roll off near Nyquist
    return amps


def _flute_amps(f: float, sr: int) -> np.ndarray:
    amps = np.array([1.0, 0.22, 0.09, 0.03])
    n = max(1, min(len(amps), int(0.45 * sr / f)))
    return amps[:n]


def _merge(events):
    """Tie-merged notes [(start_q, dur_q, midi)] and per-event (start_q, end_q)."""
    notes, spans = [], []
    t = 0.0
    cur = None
    tie = False
    for e in events:
        d = float(e.get("dur") or 0)
        spans.append((t, t + d))
        if e.get("kind") == "note" and e.get("midi") is not None:
            if cur is not None and tie and cur[2] == e["midi"]:
                cur[1] += d
            else:
                if cur is not None:
                    notes.append(cur)
                cur = [t, d, e["midi"]]
            tie = bool(e.get("tie_next"))
            if not tie:
                notes.append(cur)
                cur = None
        else:
            if cur is not None:
                notes.append(cur)
            cur, tie = None, False
        t += d
    if cur is not None:
        notes.append(cur)
    return notes, spans, t


def _envelope(n_on: int, n_rel: int, sr: int, attack: float, release: float) -> np.ndarray:
    a = max(1, int(attack * sr))
    env = np.ones(n_on + n_rel, np.float32)
    ramp = np.linspace(0, 1, min(a, n_on), dtype=np.float32)
    env[: len(ramp)] = ramp * ramp * (3 - 2 * ramp)  # smoothstep attack
    # gentle settle after the attack
    tt = np.arange(n_on, dtype=np.float32) / sr
    env[:n_on] *= 0.88 + 0.12 * np.exp(-tt / 0.6)
    if n_rel:
        last = env[n_on - 1] if n_on else 1.0
        env[n_on:] = last * np.exp(-np.arange(n_rel, dtype=np.float32) / (release * sr / 4.6))
    return env


def _render_melody(notes, spq: float, total: int, offset: int, sr: int, instrument: str) -> np.ndarray:
    out = np.zeros(total, np.float32)
    release = 0.09 if instrument == "harmonium" else 0.12
    attack = 0.03 if instrument == "harmonium" else 0.06
    n_rel = int(release * sr)
    cache = {}
    for start_q, dur_q, m in notes:
        f = _freq(m)
        s0 = offset + int(round(start_q * spq * sr))
        n_on = max(1, int(round(dur_q * spq * sr)) - int(0.012 * sr))
        n = n_on + n_rel
        if s0 >= total:
            continue
        n = min(n, total - s0)
        env = _envelope(n_on, n_rel, sr, attack, release)[:n]
        if instrument == "flute":
            key = ("fl", m)
            if key not in cache:
                cache[key] = _table(_flute_amps(f, sr))
            tt = np.arange(n, dtype=np.float32) / sr
            depth = np.clip((tt - 0.2) / 0.3, 0, 1) * 0.0069  # +/-12 cents after 200 ms
            freq = f * (1 + depth * np.sin(2 * np.pi * 5.0 * tt))
            sig = _osc(cache[key], freq, n, sr)
            noise = _rng.standard_normal(n).astype(np.float32)
            noise = np.convolve(noise, np.ones(6, np.float32) / 6, mode="same")
            sig = sig + 0.06 * noise * (0.4 + 0.6 * np.exp(-tt / 0.15))
        else:
            key = ("hm", m)
            if key not in cache:
                cache[key] = _table(_harmonium_amps(f, sr))
            tab = cache[key]
            sig = 0.55 * _osc(tab, f * 1.0023, n, sr, 0.0) + 0.55 * _osc(tab, f / 1.0023, n, sr, 0.37)
        out[s0:s0 + n] += sig * env
    # bellows: slow swell
    tt = np.arange(total, dtype=np.float32) / sr
    if instrument == "harmonium":
        out *= 1 + 0.06 * np.sin(2 * np.pi * 0.25 * tt)
    return out


def _tanpura_cycle(sa_midi: int, kind: str, sr: int, cycle: float, tail: float) -> np.ndarray:
    first = sa_midi - 5 if kind == "pa" else sa_midi - 7
    strings = [first, sa_midi, sa_midi, sa_midi - 12]
    offsets = [0.0, 0.72, 1.32, 1.92]
    n = int((cycle + tail) * sr)
    out = np.zeros(n, np.float32)
    for m, off, det in zip(strings, offsets, (1.0, 1.0012, 0.9991, 1.0)):
        f = _freq(m) * det
        nh = max(1, min(30, int(0.4 * sr / f)))
        k = np.arange(1, nh + 1)
        bright = _table(k ** -0.9 * np.exp(-k * f / 2500))
        mellow = _table(k ** -1.3)
        ln = int(tail * sr)
        tt = np.arange(ln, dtype=np.float32) / sr
        decay = np.exp(-6.9 * tt / 4.8)
        jawari = np.clip(tt / 0.35, 0, 1) * np.exp(-tt / 1.6)  # buzz swells then fades
        # keep the buzz subtle: strong swelling overtones sound like extra notes
        sig = _osc(bright, f, ln, sr) * (0.12 + 0.3 * jawari) + _osc(mellow, f, ln, sr)
        pluck = np.minimum(1, tt / 0.015) ** 2
        s0 = int(off * sr)
        out[s0:s0 + ln] += (sig * decay * pluck)[: n - s0] * (0.8 if m == sa_midi - 12 else 1.0)
    return out


def _render_drone(sa_midi: int, kind: str, total: int, sr: int) -> np.ndarray:
    cycle, tail = 2.6, 5.0
    one = _tanpura_cycle(sa_midi, kind, sr, cycle, tail)
    hop = int(cycle * sr)
    out = np.zeros(total + len(one), np.float32)
    for s0 in range(0, total, hop):
        out[s0:s0 + len(one)] += one
    out = out[:total]
    return out / (np.abs(out).max() + 1e-9)


def _render_click(total: int, sr: int, lead: int, spq: float, length_q: float, bar_q: float) -> np.ndarray:
    out = np.zeros(total, np.float32)
    n = int(0.03 * sr)
    tt = np.arange(n, dtype=np.float32) / sr
    hi = np.sin(2 * np.pi * 1800 * tt) * np.exp(-tt / 0.006)
    lo = np.sin(2 * np.pi * 1100 * tt) * np.exp(-tt / 0.005) * 0.6
    q = 0.0
    while q < length_q + 1e-6:
        s0 = lead + int(round(q * spq * sr))
        accent = bar_q > 0 and abs((q / bar_q) - round(q / bar_q)) < 1e-6
        if s0 + n < total:
            out[s0:s0 + n] += hi if accent else lo
        q += 1.0
    return out


def _reverb(x: np.ndarray, sr: int, wet=0.16, seconds=1.1) -> np.ndarray:
    n = int(seconds * sr)
    tt = np.arange(n, dtype=np.float32) / sr
    ir = _rng.standard_normal(n).astype(np.float32) * np.exp(-6.9 * tt / seconds)
    ir[: int(0.012 * sr)] = 0
    ir /= np.sqrt(np.sum(ir ** 2)) + 1e-9
    L = len(x) + n - 1
    nfft = 1 << (L - 1).bit_length()
    y = np.fft.irfft(np.fft.rfft(x, nfft) * np.fft.rfft(ir, nfft), nfft)[: len(x)].astype(np.float32)
    return (1 - wet) * x + wet * y * 0.6


def render(events, sa_midi, tempo_bpm, sr=32000, instrument="harmonium", drone=False, drone_kind="pa",
           drone_level=0.2, click=False, beats_per_bar=4, lead_in=2.0, tail=2.0):
    """Render events to mono float32 audio. Returns (audio, [(start_sec, end_sec) per event])."""
    events = [dict(e, midi=None if e.get("midi") is None else e["midi"] - 60 + sa_midi) for e in events]
    spq = 60.0 / float(tempo_bpm)
    notes, spans, length_q = _merge(events)
    lead = int(lead_in * sr)
    total = lead + int((length_q * spq + tail) * sr)
    mel = _render_melody(notes, spq, total, lead, sr, "flute" if instrument == "flute" else "harmonium")
    mel /= np.abs(mel).max() + 1e-9
    mix = mel.copy()
    if drone:
        mix += drone_level * _render_drone(sa_midi, drone_kind, total, sr)
    if click:
        mix += 0.25 * _render_click(total, sr, lead, spq, length_q, float(beats_per_bar))
    mix = _reverb(mix, sr)
    fade = int(0.02 * sr)
    mix[:fade] *= np.linspace(0, 1, fade, dtype=np.float32)
    mix[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
    mix = np.nan_to_num(mix)
    mix = (0.9 * mix / (np.abs(mix).max() + 1e-9)).astype(np.float32)
    timings = [(round(lead_in + a * spq, 4), round(lead_in + b * spq, 4)) for a, b in spans]
    return mix, timings


def write_wav(path, audio, sr):
    pcm = (np.clip(audio, -1, 1) * 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sr))
        w.writeframes(pcm.tobytes())


def _vlq(n: int) -> bytes:
    out = [n & 0x7F]
    n >>= 7
    while n:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    return bytes(reversed(out))


def write_midi(path, events, sa_midi, tempo_bpm, program=20):
    """Standard MIDI file (format 0) of the melody in the chosen key, with lyrics."""
    tpq = 480
    shifted = [dict(e, midi=None if e.get("midi") is None else e["midi"] - 60 + sa_midi) for e in events]
    notes, spans, _ = _merge(shifted)
    lyric_at = {}
    for e, (a, _b) in zip(events, spans):
        if e.get("kind") == "note" and e.get("lyric"):
            lyric_at.setdefault(int(round(a * tpq)), e["lyric"])
    msgs = []  # (tick, order, bytes)
    for start, dur, m in notes:
        on, off = int(round(start * tpq)), int(round((start + dur) * tpq))
        m = int(max(0, min(127, m)))
        msgs.append((on, 2, bytes([0x90, m, 90])))
        msgs.append((max(on + 1, off - 4), 1, bytes([0x80, m, 0])))
    for tick, text in lyric_at.items():
        t = text.encode("utf-8")[:120]
        msgs.append((tick, 0, b"\xff\x05" + _vlq(len(t)) + t))
    msgs.sort(key=lambda x: (x[0], x[1]))
    us = int(round(60_000_000 / float(tempo_bpm)))
    track = b"\x00\xff\x51\x03" + us.to_bytes(3, "big") + b"\x00" + bytes([0xC0, program & 0x7F])
    last = 0
    for tick, _, data in msgs:
        track += _vlq(tick - last) + data
        last = tick
    track += b"\x00\xff\x2f\x00"
    with open(path, "wb") as f:
        f.write(b"MThd" + struct.pack(">IHHH", 6, 0, 1, tpq))
        f.write(b"MTrk" + struct.pack(">I", len(track)) + track)
