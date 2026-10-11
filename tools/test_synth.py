"""Synth checks for flute, piano and harmonium: timing, speed, no clipping/NaN/clicks, MIDI/WAV output, seamless
long notes, piano repeated notes without a silent gap, the offline fallback, and that the audio is in the requested key.

Piano and harmonium use recorded samples that are downloaded on first use (about 3 MB each). Without internet
the sample-specific checks are skipped (with a message) and the synthesized fallback is tested instead.
Run from the repo:  uv run python tools/test_synth.py
"""
import os, sys, tempfile, time, wave
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numpy as np
from bhajan import synth, audio, instruments

SR = 32000


def max_jump(y):
    return float(np.abs(np.diff(y)).max())


def isolated_clicks(y):
    """Samples whose 2nd difference is > 10x the local RMS of the 2nd difference (discontinuities)."""
    d2 = np.abs(np.diff(y.astype(np.float64), 2))
    loc = np.sqrt(np.convolve(d2 ** 2, np.ones(641) / 641, "same")) + 1e-6
    return int((d2 > 10 * loc).sum())


sampled = []
for inst in ("piano", "harmonium"):
    t = time.time()
    try:
        instruments.ensure(inst)
        sampled.append(inst)
        print(f"{inst}: recorded samples ready in {time.time() - t:.2f}s")
    except instruments.Unavailable as e:
        print(f"SKIP sampled {inst} checks (samples could not be downloaded: {e}); testing the synthesized fallback")

ev = [{"kind": "note", "dur": 1.0, "midi": m} for m in (60, 62, 64, 65, 67, 69, 71, 72)]
ev += [{"kind": "rest", "dur": 1.0, "midi": None},
       {"kind": "note", "dur": 1.0, "midi": 67, "tie_next": True}, {"kind": "note", "dur": 1.0, "midi": 67, "lyric": "la"}]
total_q = sum(e["dur"] for e in ev)
ref_tm = None
for inst in synth.INSTRUMENTS:
    for drone in (True, False):
        y, tm = synth.render(ev, 62, 100, instrument=inst, drone=drone, click=not drone)
        assert y.dtype == np.float32 and y.ndim == 1 and np.isfinite(y).all() and 0.5 < np.abs(y).max() <= 0.91, inst
        assert abs(len(y) / SR - (2 + total_q * 0.6 + 2)) < 0.1, len(y) / SR
        assert len(tm) == len(ev) and abs(tm[0][0] - 2.0) < 1e-3 and all(a[0] <= b[0] for a, b in zip(tm, tm[1:]))
        ref_tm = ref_tm or tm
        assert tm == ref_tm, f"{inst}: timings must not depend on the instrument"
        assert max_jump(y) < 0.3, (inst, drone, max_jump(y))
        if drone:  # (without the drone the metronome click is on, and its ticks are meant to be sharp)
            assert isolated_clicks(y) <= (2 if inst == "flute" else 0), (inst, isolated_clicks(y))
print(f"render checks OK for {', '.join(synth.INSTRUMENTS)} (shape, NaN, peak, length, timings, no clicks)")

# the offline fallback: if the samples cannot be loaded the render must still succeed
real = instruments.render_notes
try:
    def _offline(*a, **k):
        raise instruments.Unavailable("simulated offline")
    instruments.render_notes = _offline
    for inst in ("piano", "harmonium"):
        y, tm = synth.render(ev, 62, 100, instrument=inst)
        assert np.isfinite(y).all() and 0.5 < np.abs(y).max() <= 0.91 and tm == ref_tm, inst
finally:
    instruments.render_notes = real
print("offline fallback OK (synthesized piano/harmonium)")

if sampled:
    # long notes: the harmonium loops seamlessly; the piano keeps decaying; ties do not re-strike
    for inst in sampled:
        for m in (55, 62, 69, 76, 83):
            y, _ = synth.render([{"kind": "note", "dur": 16.0, "midi": m}], 60, 120, instrument=inst)  # 8 s note
            # (bright high reed notes legitimately have sample steps up to ~0.4, so look for discontinuities)
            assert isolated_clicks(y) == 0 and max_jump(y) < 0.6, (inst, m, max_jump(y))
            hop = int(0.05 * SR)
            seg = y[int(2.5 * SR): int(9.5 * SR)].astype(np.float64)
            db = 10 * np.log10((seg[: len(seg) // hop * hop].reshape(-1, hop) ** 2).mean(axis=1) + 1e-15)
            if inst == "harmonium":
                assert db.std() < 1.5, (m, db.std())           # steady (only the gentle bellows swell)
            else:
                assert np.all(np.diff(db) < 3.0) and db[-1] < db[0] - 20, (m, db[0], db[-1])  # natural decay
        tie = [{"kind": "note", "dur": 1.0, "midi": 64, "tie_next": True}, {"kind": "note", "dur": 1.0, "midi": 64}]
        y, tm = synth.render(tie, 60, 60, instrument=inst)
        w = int(0.03 * SR)
        def rms(t):
            return np.sqrt(np.mean(y[int(t * SR): int(t * SR) + w].astype(np.float64) ** 2))
        assert rms(tm[1][0]) < 1.25 * rms(tm[1][0] - 0.05), f"{inst}: a tied note must not be struck again"
    print(f"long-note / tie checks OK for {', '.join(sampled)}")

if "piano" in sampled:
    # Repeated notes (S S S ...) are struck again while the string still rings, as on a real piano: the level only
    # dips a little before the new attack. (It used to damp to silence first, a ~40 dB, ~12 ms hole that made the
    # piano sound choppy.) Legato between different notes stays seamless. Measured on the melody buffer (no reverb).
    def boundary_dips(y, notes, spq, lead):
        """(same pitch?, dip dB, ms more than 20 dB down) at each note that starts where the previous one ends."""
        w = int(0.01 * SR)
        c = np.concatenate([[0.0], np.cumsum(y.astype(np.float64) ** 2)])
        def db(t):  # 10 ms RMS level centred at the times t (s)
            i = (np.atleast_1d(t) * SR).astype(int)
            return 10 * np.log10((c[i + w // 2] - c[i - w // 2]) / w + 1e-12)
        out = []
        for (s0, d0, m0), (s1, d1, m1) in zip(notes, notes[1:]):
            if abs(s0 + d0 - s1) > 1e-9:
                continue
            T, end = lead / SR + s1 * spq, lead / SR + (s1 + d1) * spq
            e = db(T + np.arange(-40, 41) / 1000)
            ref = min(db(T - 0.035)[0], db(np.arange(T, min(T + 0.08, end - 0.005), 0.001)).max())
            out.append((m0 == m1, ref - e[10:71].min(), int((e < ref - 20).sum())))
        return out
    rows = []
    for m in (52, 57, 60, 64, 67, 72, 76):  # every sample zone a melody uses
        mel = [(0, 1, m), (1, 1, m), (2, .5, m), (2.5, .5, m), (3, .5, m), (3.5, 1.5, m + 2), (5, 1, m + 4),
               (6, 3, m + 4), (9, 1, m + 2), (10, .25, m + 2), (10.25, .25, m + 2), (10.5, 1.5, m)]
        for bpm in (70, 120):
            spq = 60 / bpm
            y = instruments.render_notes("piano", mel, spq, SR + int((12 * spq + 1) * SR), SR, SR)
            rows += boundary_dips(y, mel, spq, SR)
    same = [r for r in rows if r[0]]
    assert max(r[1] for r in same) < 15 and max(r[2] for r in same) == 0, ("piano re-strike hole", max(same, key=lambda r: r[1]))
    assert min(r[1] for r in same) > 3, ("a repeated piano note must still be heard as a new note", min(same, key=lambda r: r[1]))
    assert max(r[1] for r in rows if not r[0]) < 8, "piano legato between different notes must stay smooth"
    print(f"piano repeated-note checks OK (re-strike dip {np.median([r[1] for r in same]):.1f} dB median, "
          f"max {max(r[1] for r in same):.1f} dB; no silent gap)")

with tempfile.TemporaryDirectory() as td:
    y, _ = synth.render(ev, 62, 100, instrument="flute")
    synth.write_wav(os.path.join(td, "a.wav"), y, SR)
    synth.write_midi(os.path.join(td, "a.mid"), ev, 62, 100)
    with wave.open(os.path.join(td, "a.wav")) as w:
        assert w.getframerate() == SR and w.getnframes() == len(y)
    data = open(os.path.join(td, "a.mid"), "rb").read()
    assert data[:4] == b"MThd" and b"\xc0\x14" in data  # default program: reed organ (20), as before
    for inst, prog in (("flute", 73), ("piano", 0), ("harmonium", 20)):
        synth.write_midi(os.path.join(td, "b.mid"), ev, 62, 100, instrument=inst)
        assert bytes([0xC0, prog]) in open(os.path.join(td, "b.mid"), "rb").read(), inst
    # key check: a 3-minute melody in the C-written scale rendered with Sa = A must be detected as A
    long = [{"kind": "note", "dur": 0.5, "midi": 60 + [0, 2, 4, 5, 7, 9, 11, 12, 7, 4, 0][i % 11]} for i in range(720)]
    for inst in synth.INSTRUMENTS:
        for drone in ((True, False) if inst == "flute" else (False,)):
            t = time.time()
            y, _ = synth.render(long, 57, 120, instrument=inst, drone=drone)
            el = time.time() - t
            synth.write_wav(os.path.join(td, "b.wav"), y, SR)
            k = audio.analyze_key(os.path.join(td, "b.wav"), ["S", "R2", "G2", "M1", "P", "D2", "N2"])
            kind = "samples" if inst in sampled else ("synth fallback" if inst != "flute" else "synth")
            print(f"{inst:9s} ({kind}{', drone' if drone else ''}): {len(y)/SR:.0f}s of audio in {el:.2f}s; "
                  f"detected Sa = {k['sa_name']} ({k['tuning_cents']:+.0f} cents); max |dx| {max_jump(y):.3f}")
            assert el < 10 and k["sa_name"] == "A" and abs(k["tuning_cents"]) < 10, (inst, k)
print("ALL SYNTH TESTS PASSED")
