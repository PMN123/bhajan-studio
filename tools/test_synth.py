"""Synth checks: timing, speed, no clipping/NaN, MIDI/WAV output, and that the audio is in the requested key."""
import os, sys, tempfile, time, wave
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numpy as np
from bhajan import synth, audio

ev = [{"kind": "note", "dur": 1.0, "midi": m} for m in (60, 62, 64, 65, 67, 69, 71, 72)]
ev += [{"kind": "rest", "dur": 1.0, "midi": None},
       {"kind": "note", "dur": 1.0, "midi": 67, "tie_next": True}, {"kind": "note", "dur": 1.0, "midi": 67, "lyric": "la"}]
total_q = sum(e["dur"] for e in ev)
for inst in ("harmonium", "flute"):
    for drone in (True, False):
        y, tm = synth.render(ev, 62, 100, instrument=inst, drone=drone, click=not drone)
        assert y.dtype == np.float32 and y.ndim == 1 and np.isfinite(y).all() and np.abs(y).max() <= 0.91
        assert abs(len(y) / 32000 - (2 + total_q * 0.6 + 2)) < 0.1, len(y) / 32000
        assert len(tm) == len(ev) and abs(tm[0][0] - 2.0) < 1e-3 and all(a[0] <= b[0] for a, b in zip(tm, tm[1:]))
with tempfile.TemporaryDirectory() as td:
    y, _ = synth.render(ev, 62, 100)
    synth.write_wav(os.path.join(td, "a.wav"), y, 32000)
    synth.write_midi(os.path.join(td, "a.mid"), ev, 62, 100)
    with wave.open(os.path.join(td, "a.wav")) as w:
        assert w.getframerate() == 32000 and w.getnframes() == len(y)
    assert open(os.path.join(td, "a.mid"), "rb").read(4) == b"MThd"
    # key check: a long melody in the C-written scale rendered with Sa = A must be detected as A
    long = [{"kind": "note", "dur": 0.5, "midi": 60 + [0, 2, 4, 5, 7, 9, 11, 12, 7, 4, 0][i % 11]} for i in range(360)]
    t = time.time()
    y, _ = synth.render(long, 57, 120, drone=True)
    el = time.time() - t
    synth.write_wav(os.path.join(td, "b.wav"), y, 32000)
    k = audio.analyze_key(os.path.join(td, "b.wav"), ["S", "R2", "G2", "M1", "P", "D2", "N2"])
    print(f"render {len(y)/32000:.0f}s of audio in {el:.2f}s; detected Sa = {k['sa_name']} ({k['tuning_cents']:+.0f} cents)")
    assert el < 10 and k["sa_name"] == "A" and abs(k["tuning_cents"]) < 10
print("ALL SYNTH TESTS PASSED")
