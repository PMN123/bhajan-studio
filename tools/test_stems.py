"""Parts checks that need neither the separation models nor a GPU: a fake split (synthetic singing,
tabla, harmonium and a near-silent remainder, written exactly as stems.split() writes them) is read,
mixed, shifted, cached, exported as a session and served through the API. The split queue is driven
with a stand-in child process, the child's signal handling with a stand-in separator, the model download
with a server on 127.0.0.1, and the loudness meter is checked against the ITU-R BS.1770 and
EBU Tech 3341 reference values.

The API checks use FastAPI's TestClient, which needs httpx2 or httpx, and the download checks need the 'stems'
extra; without them those checks are skipped with a message.
Run from the repo:  uv run --frozen --extra stems --with httpx python tools/test_stems.py
"""
import json, math, os, shutil, signal, subprocess, sys, tempfile, threading, time, warnings
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import numpy as np
import scipy.signal as ss
from bhajan import stems, store

SR = 22050          # most Sai Rhythms recordings are 22 kHz mono
N = 4 * SR
TMP = tempfile.TemporaryDirectory(prefix="bhajan-stems-test-")
td = TMP.name
failed, _mark = [], 0


def check(ok, what, detail=""):
    """Record one check; failures are listed at the end instead of stopping the run."""
    if not ok:
        failed.append(what)
        print(f"FAIL {what}" + (f" ({detail})" if detail else ""))
    return ok


def section(name, detail=""):
    global _mark
    bad, _mark = len(failed) - _mark, len(failed)
    print(f"{name} checks " + (f"OK ({detail})" if not bad else f"FAILED ({bad})"))


def wait_for(cond, timeout=30.0):
    end = time.time() + timeout
    while not cond() and time.time() < end:
        time.sleep(0.05)
    return cond()


def rms(x):
    return float(np.sqrt(np.mean(np.asarray(x, np.float64) ** 2)))


# ------------------------------------------------------------------ a fake recording, split into parts
t = np.arange(N) / SR
rng = np.random.default_rng(7)
LINE = [0, 2, 4, 5, 7, 5, 4, 2]          # Sa Re Ga Ma Pa Ma Ga Re, a quarter second each, Sa = 220 Hz


def voice(start, cents=0.0, vib_phase=0.0):
    """A sung line from `start` s: 10 harmonics, 5.5 Hz vibrato, a soft attack and release on each note."""
    f, env = np.zeros(N), np.zeros(N)
    att, rel = int(0.015 * SR), int(0.04 * SR)
    for k, st in enumerate(LINE):
        a, b = int((start + 0.25 * k) * SR), int((start + 0.25 * (k + 1)) * SR)
        f[a:b] = 220.0 * 2 ** ((st + cents / 100) / 12)
        env[a:a + att], env[a + att:b - rel], env[b - rel:b] = np.linspace(0, 1, att), 1, np.linspace(1, 0, rel)
    ph = 2 * np.pi * np.cumsum(f * 2 ** (0.3 / 12 * np.sin(2 * np.pi * 5.5 * t + vib_phase))) / SR
    return env * sum(np.sin(h * ph) / h for h in range(1, 11))


def tabla():
    """16 strokes: a 10 ms noise slap, then a ringing 'na' (330 Hz) or a booming 'ge' (95 Hz)."""
    y = np.zeros(N)
    m = int(0.2 * SR)
    tt = np.arange(m) / SR
    for k in range(16):
        s = 0.5 * rng.standard_normal(m) * np.exp(-tt / 0.01)
        s += np.sin(2 * np.pi * (330 if k % 2 else 95) * tt) * np.exp(-tt / (0.12 if k % 2 else 0.2))
        a = int(0.25 * k * SR)
        y[a:a + m] += (1.0 if k % 4 == 0 else 0.6) * s
    return y


def harmonium():
    """A held Sa-Ga-Pa chord of reeds with a slow bellows swell."""
    y = sum(np.sin(2 * np.pi * h * f0 * t) / h ** 1.2 for f0 in (220.0, 277.18, 329.63) for h in range(1, 13))
    fade = np.minimum(1, np.minimum(t, t[-1] - t) / 0.05)
    return y * fade * (0.8 + 0.2 * np.sin(2 * np.pi * 0.5 * t))


def make_parts():
    """{part: full-level mono float32}; the singing is a lead line answered by the group (call and response)."""
    def norm(x):
        return x / np.abs(x).max()
    p = {"voice": 0.30 * norm(voice(0.0)) + 0.25 * norm(voice(2.0, 8, 1.0) + voice(2.0, -8, 2.5)),
         "tabla": 0.30 * norm(tabla()),
         "harmonium": 0.15 * norm(harmonium()),
         "other": 1e-4 * rng.standard_normal(N)}
    k = 0.9 / np.abs(sum(p.values())).max()          # the "recording" peaks at 0.9
    return {name: (k * x).astype(np.float32) for name, x in p.items()}


def fake_split(d, parts):
    """Write `parts` (at full level) into d the way stems.split() does: FLACs at -6 dB plus manifest.json."""
    os.makedirs(d, exist_ok=True)
    mix = sum(parts[p].astype(np.float64) for p in stems.PARTS)
    m = {"version": stems.STEMS_VERSION, "sr": SR, "channels": 1, "frames": N, "gain": 1 / stems.HEADROOM,
         "rms": rms(mix), "parts": {}, "models": [s["model"] for s in stems.CASCADE], "seconds": 1.0}
    for p in stems.PARTS:
        stems.write_flac(os.path.join(d, p + ".flac"), (stems.HEADROOM * parts[p])[:, None], SR)
        share = float((parts[p].astype(np.float64) ** 2).sum() / (mix ** 2).sum())
        m["parts"][p] = {"file": p + ".flac", "share_db": round(10 * math.log10(share + 1e-12), 1)}
    store.write_json(os.path.join(d, "manifest.json"), m)
    return m


parts = make_parts()
mix = sum(parts[p] for p in stems.PARTS)
d = os.path.join(td, "split")
m = fake_split(d, parts)
print("fake split: " + ", ".join(f"{p} {m['parts'][p]['share_db']:+.1f} dB" for p in stems.PARTS))

# ------------------------------------------------------------------ a) manifest
check(stems.read_manifest(d) == m, "read_manifest accepts a folder written like split()")
bad = os.path.join(td, "bad")
shutil.copytree(d, bad)
store.write_json(os.path.join(bad, "manifest.json"), m | {"version": "cascade-0"})
check(stems.read_manifest(bad) is None, "read_manifest rejects another STEMS_VERSION")
store.write_json(os.path.join(bad, "manifest.json"), m)
os.remove(os.path.join(bad, "tabla.flac"))
check(stems.read_manifest(bad) is None, "read_manifest rejects a missing part file")
with open(os.path.join(bad, "manifest.json"), "w") as f:
    f.write('{"version": ')
check(stems.read_manifest(bad) is None, "read_manifest rejects a damaged manifest")
check(stems.read_manifest(os.path.join(td, "nowhere")) is None, "read_manifest of a missing folder is None")
section("manifest", "accepted; wrong version, missing part, damaged file rejected")

# ------------------------------------------------------------------ b) FLAC round trip and probe
sweep = 0.999 * ss.chirp(np.arange(N) / 44100, 50, N / 44100, 15000).astype(np.float32)
for sr, x in ((SR, (0.5 * parts["voice"])[:, None]), (44100, np.stack([sweep, 0.5 * parts["tabla"]], axis=1))):
    path = os.path.join(td, f"rt{x.shape[1]}.flac")
    stems.write_flac(path, x, sr)
    check(stems.probe(path) == (sr, x.shape[1]), f"probe reads {sr} Hz / {x.shape[1]} ch", stems.probe(path))
    y, r = stems.read_audio(path)
    err = float(np.abs(y - x).max()) if y.shape == x.shape else float("inf")
    check(r == sr and y.shape == x.shape, f"read_audio returns {x.shape} at {sr} Hz", f"{y.shape} at {r} Hz")
    # 24-bit: the error is one quantisation step (2^-23 of full scale) at most, and not zero (not float)
    check(0 < err <= 2 ** -23 and err / np.abs(x).max() < 1e-6, f"24-bit FLAC round trip ({x.shape[1]} ch)",
          f"max error {err:.2e}")
section("FLAC", "24-bit round trip error < 1e-6 relative; probe right for mono 22.05 kHz and stereo 44.1 kHz")

# ------------------------------------------------------------------ c) loudness (BS.1770)
# ITU-R BS.1770-5 (11/2023), Annex 1, after eq. (2): "If a 0 dB FS, 1 kHz (997 Hz to be exact, see Notes 1
# and 2) sine wave is applied to the left, centre, or right channel input, the indicated loudness will equal
# -3.01 LKFS." (Note 1: the -0.691 constant cancels the K-weighting gain at 997 Hz.)
# EBU Tech 3341 (2023), Table 1 "Minimum requirements test signals": 1000 Hz stereo sines, per-channel peak;
# case 1: -23 dBFS 20 s -> I = -23.0 +-0.1 LUFS; case 3: -36/-23/-36 dBFS for 10/60/10 s; case 4: -72/-36/-23/
# -36/-72 dBFS for 10/10/60/10/10 s; case 5: -26/-20/-26 dBFS for 20/20.1/20 s -> each I = -23.0 +-0.1 LUFS.
def tone(db, sec, sr=48000, hz=1000.0):
    return (10 ** (db / 20) * np.sin(2 * np.pi * hz * np.arange(int(round(sec * sr))) / sr)).astype(np.float32)


def stereo(*segments):
    x = np.concatenate([tone(db, sec) for db, sec in segments])
    return np.stack([x, x], axis=1)


# Regression: the K-weighting shelf once took libebur128's parameters (f0 1681.97 Hz, G 3.9998 dB, Q 0.70718)
# but built the filter with the RBJ cookbook formula, so its gain at 997 Hz was +0.43 dB instead of +0.69 dB
# and every reading was ~0.26 LU low (-3.27 below; the -18 LUFS session target came out ~-17.75 LUFS).
# _k_weighting now designs both filters as libebur128 does, which gives BS.1770's own table at 48 kHz.
BS1770_48K = ([1.53512485958697, -2.69169618940638, 1.19839281085285], [1.0, -1.69065929318241, 0.73248077421585],
              [1.0, -2.0, 1.0], [1.0, -1.99004745483398, 0.99007225036621])     # Tables 1 and 2
kw = [c for ba in stems._k_weighting(48000) for c in ba]
check(all(np.allclose(c, want, rtol=0, atol=1e-12) for c, want in zip(kw, BS1770_48K)),
      "K-weighting at 48 kHz has BS.1770's coefficients (Tables 1 and 2)", kw)
for sr in (48000, SR):
    s = tone(0.0, 10, sr, 997.0)
    lu = stems.loudness(np.stack([s, np.zeros_like(s)], axis=1), sr)
    check(abs(lu - -3.01) <= 0.1, f"BS.1770: full-scale 997 Hz sine in one channel at {sr} Hz reads -3.01 LUFS",
          f"reads {lu:.2f} LUFS; ~-3.27 means the K-weighting shelf is off again (see the note above)")
case1 = stems.loudness(stereo((-23, 20)), 48000)
for name, segs in (("3", ((-36, 10), (-23, 60), (-36, 10))),
                   ("4", ((-72, 10), (-36, 10), (-23, 60), (-36, 10), (-72, 10))),
                   ("5", ((-26, 20), (-20, 20.1), (-26, 20)))):
    lu = stems.loudness(stereo(*segs), 48000)
    # all of Tech 3341's cases read -23.0, so the gated cases must match case 1 whatever the calibration
    check(abs(lu - case1) <= 0.1, f"EBU Tech 3341 case {name} (gating) reads like case 1", f"{lu:.2f} vs {case1:.2f}")
mono = (0.1 * np.sin(2 * np.pi * 997 * t)).astype(np.float32)[:, None]        # -20 dBFS peak
for name, x in (("997 Hz sine", mono), ("voice part", parts["voice"][:, None])):
    diff = stems.loudness(np.repeat(x, 2, axis=1), SR) - stems.loudness(x, SR)
    check(abs(diff - 3.01) <= 0.1, f"{name}: mono duplicated to stereo reads +3.01 LU", f"{diff:+.2f} LU")
check(stems.loudness(np.zeros((5 * SR, 2), np.float32), SR) == -70.0, "silence reads -70 LUFS")
check(stems.loudness(mono[:int(0.3 * SR)], SR) == -70.0, "less than one 400 ms block reads -70 LUFS")
section("loudness", "BS.1770 coefficients and -3.01 LUFS reference, EBU Tech 3341 gating, +3.01 LU mono->stereo, silence")

# ------------------------------------------------------------------ d) mixing
paths0 = {p: os.path.join(d, m["parts"][p]["file"]) for p in stems.PARTS}
read = {p: stems.read_audio(paths0[p])[0] for p in stems.PARTS}
y, r = stems.mix_parts(paths0, {}, m["gain"])
ref = None
for p in stems.PARTS:
    ref = read[p] * np.float32(m["gain"]) if ref is None else ref + read[p] * np.float32(m["gain"])
check(r == SR and y.shape == (N, 1), "mix_parts returns (frames, 1) at the parts' rate", f"{y.shape} at {r} Hz")
check(np.abs(y - ref).max() <= 1e-6, "every part at 0 dB sums to the parts x gain", f"{np.abs(y - ref).max():.1e}")
check(np.abs(y[:, 0] - mix).max() <= 1e-5, "every part at 0 dB gives back the original recording",
      f"max error {np.abs(y[:, 0] - mix).max():.1e}")
y2, _ = stems.mix_parts(paths0, {"voice": None}, m["gain"])
check(np.abs((y - y2) - 2 * read["voice"]).max() <= 1e-6, "muting (None) removes exactly that part")
y3, _ = stems.mix_parts(paths0, {"harmonium": -6.0}, m["gain"])
want = y - 2 * read["harmonium"] * np.float32(1 - 10 ** (-6 / 20))
check(np.abs(y3 - want).max() <= 1e-6, "a part at -6 dB is scaled by 10^(-6/20)")
try:
    stems.mix_parts(paths0, {p: None for p in stems.PARTS}, m["gain"])
    check(False, "every part muted raises ValueError")
except ValueError:
    pass
section("mix", "0 dB reconstructs the recording, mute, gain, all muted -> ValueError")

# ------------------------------------------------------------------ e) shifting one part
def peak_hz(x, sr):
    """Frequency of the strongest FFT peak of the middle half, refined by a parabola through the log magnitudes."""
    x = x[len(x) // 4: 3 * len(x) // 4] * np.hanning(len(x) // 2)
    s = np.abs(np.fft.rfft(x, 8 * len(x)))
    k = int(np.argmax(s))
    a, b, c = np.log(s[k - 1:k + 2])
    return (k + 0.5 * (a - c) / (a - 2 * b + c)) * sr / (8 * len(x))


def lag_ms(x, y, sr):
    """Delay of y against x from the cross-correlation of their 5 ms RMS envelopes (searched within +-50 ms)."""
    w = int(0.005 * sr)
    def env(s):
        e = np.sqrt(np.convolve(s.astype(np.float64) ** 2, np.ones(w) / w, "same"))
        return e - e.mean()
    a, b = env(x), env(y[:len(x)])
    c = ss.correlate(b, a, mode="full", method="fft")
    lags = np.arange(-len(a) + 1, len(b))
    near = np.abs(lags) <= int(0.05 * sr)
    return 1000 * lags[near][np.argmax(c[near])] / sr


x = (stems.HEADROOM * parts["voice"])[:, None]
check(stems.shift_array(x, SR, 0.0, 1.0, "voice") is x, "+0 st at 1.0x returns the input untouched")
sine = (0.25 * np.sin(2 * np.pi * 220 * np.arange(2 * SR) / SR)).astype(np.float32)[:, None]
for p in ("voice", "tabla"):          # formants kept (voices, reeds) and formants moved (the drum)
    for st in (12, -12):
        f = peak_hz(stems.shift_array(sine, SR, st, 1.0, p)[:, 0], SR)
        want = 220 * 2 ** (st / 12)
        check(abs(f / want - 1) <= 0.01, f"{p}: {st:+d} st moves a 220 Hz sine to {want:.0f} Hz", f"{f:.1f} Hz")
for tempo in (0.9, 1.2):
    y = stems.shift_array(x, SR, 0.0, tempo, "voice")
    check(abs(len(y) / len(x) * tempo - 1) <= 0.01, f"tempo {tempo} makes it {1 / tempo:.3f}x as long",
          f"{len(y) / len(x):.4f}x")
lags = {}
for p in ("tabla", "voice"):
    xp = (stems.HEADROOM * parts[p])[:, None]
    y = stems.shift_array(xp, SR, 2.0, 1.0, p)
    lags[p] = lag_ms(xp[:, 0], y[:, 0], SR)
    check(len(y) == len(xp), f"{p}: +2 st keeps the length", f"{len(y)} vs {len(xp)} frames")
    check(abs(lags[p]) <= 2.0, f"{p}: +2 st stays aligned with the original (within 2 ms)", f"{lags[p]:+.2f} ms")
    if p == "voice":
        g = 20 * math.log10(rms(y) / rms(xp))
        check(abs(g) <= 1.5, "voice: +2 st keeps the level (within 1.5 dB)", f"{g:+.2f} dB")
y = stems.shift_array(np.stack([sweep, 0.5 * parts["tabla"]], axis=1), 44100, 2.0, 1.0, "harmonium")
check(y.shape == (N, 2), "stereo parts shift as (frames, 2)", y.shape)
section("shift", "pitch x2 within 1%, tempo, alignment " + ", ".join(f"{p} {v:+.2f} ms" for p, v in lags.items()))

# ------------------------------------------------------------------ f) shifted parts are cached
p0, g0 = stems.shifted_parts(d, 0.0, 1.0)
check(p0 == paths0 and g0 == 2.0, "0 st at 1.0x uses the split's own files at the manifest's gain")
tm = time.time()
sp, sp_gain = stems.shifted_parts(d, -3.0, 1.0)
t_first = time.time() - tm
sd = os.path.join(d, "shift", stems.shift_key(-3.0, 1.0))
check(sorted(sp) == sorted(stems.PARTS) and all(os.path.dirname(v) == sd and os.path.exists(v) for v in sp.values()),
      "shifted_parts writes one FLAC per part into shift/<key>/")
check(sorted(os.listdir(sd)) == sorted([p + ".flac" for p in stems.PARTS] + ["shift.json"]), "only the parts and shift.json are left in shift/<key>/",
      os.listdir(sd))
check(all(stems.probe(v) == (SR, 1) and len(stems.read_audio(v)[0]) == N for v in sp.values()),
      "shifted parts keep the rate, channels and length")
ino = {p: os.stat(v).st_ino for p, v in sp.items()}
tm = time.time()
sp2, sp2_gain = stems.shifted_parts(d, -3.0, 1.0)
t_second = time.time() - tm
# a re-render would replace each file (store.write_with renames a new file into place), changing its inode
check(sp2 == sp and sp2_gain == sp_gain and all(os.stat(v).st_ino == ino[p] for p, v in sp2.items()), "the second call re-uses the cache")
check(t_second < t_first / 3, "the second call is fast", f"{t_second:.3f}s vs {t_first:.3f}s")
section("cache", f"first {t_first:.2f}s, cached {t_second * 1000:.0f} ms")

# ------------------------------------------------------------------ one scale for a whole shift set
# Regression: a phase vocoder can raise a part's peaks (a hard-limited, square-ish harmonium most of all). Such a
# part must neither clip in its FLAC nor be turned down on its own, which would change the balance between the
# parts the browser mixes: every part of the shift set gets one factor, kept in shift.json, and the gain
# shifted_parts returns makes up for it. The reference is the parts shifted the way shifted_parts does it: together,
# as channels of one stretcher (shifting them one by one gives their shared bleed unrelated phases).
sq = np.clip(20 * np.sin(2 * np.pi * 220 * t), -1, 1) * np.minimum(1, np.minimum(t, t[-1] - t) / 0.05)
dl = os.path.join(td, "loud")
ml = fake_split(dl, parts | {"harmonium": (0.9 / stems.HEADROOM * sq).astype(np.float32)})   # FLAC peaks at 0.9
orig = {p: stems.read_audio(os.path.join(dl, p + ".flac"))[0] for p in stems.PARTS}
over = {}
for st in (4.0, -4.0):
    joint = stems.shift_array(np.concatenate([orig[p] for p in stems.PARTS], axis=1), SR, st, 1.0)
    want = {p: joint[:, i:i + 1] for i, p in enumerate(stems.PARTS)}
    over[st] = max(float(np.abs(y).max()) for y in want.values())
    check(over[st] > 1.1, f"{st:+.0f} st: the test's harmonium does go over full scale when shifted", f"peak {over[st]:.2f}")
    sp, g = stems.shifted_parts(dl, st, 1.0)
    meta = store.read_json(os.path.join(os.path.dirname(sp["voice"]), "shift.json"))
    check(meta is not None and meta.get("gain") == g, f"{st:+.0f} st: shift.json keeps the shift set's gain", meta)
    got = {p: stems.read_audio(sp[p])[0] for p in stems.PARTS}
    peaks = {p: round(float(np.abs(y).max()), 3) for p, y in got.items()}
    check(max(peaks.values()) < 1.0, f"{st:+.0f} st: no shifted part clips in its FLAC", peaks)
    check(g > ml["gain"], f"{st:+.0f} st: the returned gain makes up for the common scale", f"{g:.3f} vs {ml['gain']}")
    db = {p: round(20 * math.log10(rms(got[p] * np.float32(g)) / rms(want[p] * np.float32(ml["gain"]))), 2)
          for p in stems.PARTS}
    check(all(abs(v) <= 0.5 for v in db.values()),
          f"{st:+.0f} st: every part x the returned gain is at the level of the part shifted x the manifest gain", db)
    y, _ = stems.mix_parts(sp, {}, g)
    ref = sum(want[p] for p in stems.PARTS) * np.float32(ml["gain"])
    err = rms(y - ref) / rms(ref) if y.shape == ref.shape else float("inf")
    check(err <= 0.01, f"{st:+.0f} st: mixing the shift set at 0 dB is the jointly shifted parts' sum at the manifest gain",
          f"relative error {err:.1e}, shapes {y.shape} {ref.shape}")
section("shift gain", "harmonium shifted to peak " + ", ".join(f"{v:.2f} at {k:+.0f} st" for k, v in over.items())
        + "; parts below full scale, balance kept")

# ------------------------------------------------------------------ g) session export
# Loudness is anchored on each bhajan's full recording: the gain that brings ALL its parts to -18 LUFS is applied to
# the chosen mix, so a mix without the singing stays as much quieter as the singing was loud (room for the hall).
items = [{"dir": d, "title": "One", "semitones": 2.0, "tempo": 1.0, "gains": {"tabla": None}},   # learn: no tabla
         {"dir": d, "title": "Two", "semitones": -3.0, "tempo": 0.9, "gains": {"voice": None}}]   # the hall sings
GAP, OUT_SR = 1.5, stems.MODEL_SR
calls = []
dst = os.path.join(td, "session.mp3")
tm = time.time()
info = stems.export_session(items, dst, gap_seconds=GAP, lufs=-18.0, progress=lambda i, title: calls.append((i, title)))
el = time.time() - tm
lens = []
for it in items:     # each item lasts as long as its longest unmuted shifted part, resampled to 44.1 kHz
    sp, _ = stems.shifted_parts(d, it["semitones"], it["tempo"])
    lens.append(2 * max(len(stems.read_audio(sp[p])[0]) for p in stems.PARTS if it["gains"].get(p, 0) is not None))
check(calls == [(0, "One"), (1, "Two"), (2, "Encoding")], "export reports progress per item", calls)
start2 = lens[0] / OUT_SR + GAP
check([tr["title"] for tr in info["tracks"]] == ["One", "Two"] and info["tracks"][0]["start"] == 0.0
      and abs(info["tracks"][1]["start"] - start2) <= 0.01, "track starts are 0 and first + gap",
      f"{info['tracks']} vs {start2:.2f}")
y, r = stems.read_audio(dst)
total = (lens[0] + lens[1]) / OUT_SR + GAP
check(r == OUT_SR and y.shape[1] == 2, "the session MP3 is 44.1 kHz stereo", f"{r} Hz, {y.shape[1]} ch")
check(abs(len(y) / r - total) <= 0.05 and abs(info["seconds"] - total) <= 0.1,
      "the session lasts both items plus the gap", f"{len(y) / r:.3f}s decoded, {info['seconds']}s reported, "
      f"{total:.3f}s expected")
s2 = int(round(info["tracks"][1]["start"] * r))
segs = {"One": y[:lens[0]], "Two": y[s2:s2 + lens[1]]}
lus = {k: stems.loudness(v, r) for k, v in segs.items()}
for it in items:     # expected: -18 LUFS plus how much quieter the chosen mix is than the full recording
    sp, g = stems.shifted_parts(d, it["semitones"], it["tempo"])
    full, fr = stems.mix_parts(sp, {}, g)
    part, _ = stems.mix_parts(sp, it["gains"], g)
    st2 = lambda x: np.repeat(x, 2, axis=1) if x.shape[1] == 1 else x
    want_lu = -18.0 + stems.loudness(st2(part), fr) - stems.loudness(st2(full), fr)
    lu = lus[it["title"]]
    check(abs(lu - want_lu) <= 0.5, f"session item '{it['title']}' sits where its full recording would be -18 LUFS",
          f"{lu:.2f} LUFS, expected {want_lu:.2f}")
gap = y[lens[0] + int(0.1 * r): s2 - int(0.1 * r)]
check(rms(gap) < 1e-3, "the gap between items is silent", f"rms {rms(gap):.1e}")
check(np.abs(y).max() <= 1.0, "the session never clips", f"peak {np.abs(y).max():.3f}")
try:
    stems.export_session([], os.path.join(td, "empty.mp3"))
    check(False, "an empty session raises ValueError")
except ValueError:
    pass
section("session export", f"{total:.1f}s in {el:.2f}s; " + ", ".join(f"{k} {v:.2f} LUFS" for k, v in lus.items()))

# ------------------------------------------------------------------ cleaning up after an interrupted split
cw = os.path.join(td, "clean")
shutil.copytree(d, cw)
kept = sorted(os.listdir(cw))                     # the parts, manifest.json and shift/
os.makedirs(os.path.join(cw, "split-k3j9x", "sub"))
for name in ("split-k3j9x/voice-in.wav", "split-k3j9x/sub/x.wav", ".voice.a8f2.part.flac", ".manifest.q1.part.json"):
    with open(os.path.join(cw, name), "wb") as f:
        f.write(b"half")
stems._clean_work(cw)
check(sorted(os.listdir(cw)) == kept and stems.read_manifest(cw) == m,
      "_clean_work removes split-* work folders and .part files, and keeps the parts", sorted(os.listdir(cw)))
stems._clean_work(os.path.join(td, "nowhere"))     # a split that never made its folder: nothing to do
section("clean up", "split-* folders and .part files removed, FLACs and manifest kept")

# ------------------------------------------------------------------ split jobs, with a stand-in for the model process
# The child prints JSON lines like `python -m bhajan.stems split` does (plus some JSON that is not an event,
# which used to crash the reader), and leaves a work folder and a half-written file in DIR, as an interrupted
# split does. What it does then depends on SRC's name: "fail" reports an error, "slow" waits to be cancelled,
# anything else copies the fake split into DIR.
fake_child = os.path.join(td, "fake_child.py")
with open(fake_child, "w") as f:
    f.write(f"""import json, os, shutil, sys, time
src, out = sys.argv[2], sys.argv[3]
def emit(ev):
    print(json.dumps(ev), flush=True)
os.makedirs(os.path.join(out, "split-abc123"), exist_ok=True)
open(os.path.join(out, ".voice.xyz.part.flac"), "w").close()
print('5\\n"text"\\n[1, 2]\\nnull', flush=True)
emit({{"stage": "voice", "step": 1, "steps": 3, "msg": "Separating the singing", "sub": "load", "frac": 0.0,
      "model_mb": 870, "cached": False}})
if "fail" in src:
    emit({{"error": "RuntimeError: no audio"}})
    sys.exit(1)
if "slow" in src:
    time.sleep(60)
emit({{"stage": "music", "step": 3, "steps": 3, "msg": "Separating tabla", "sub": "separate", "frac": 0.5}})
shutil.copytree({d!r}, out, dirs_exist_ok=True)
emit({{"manifest": {{}}}})
""")
real_child = stems.child_command
stems.child_command = lambda *args: [sys.executable, fake_child, *args]
try:
    jobs = stems.SplitJobs(os.path.join(td, "models"))
    jd = {k: os.path.join(td, "jobs", k) for k in ("ok", "slow", "queued", "fail")}
    check(jobs.status("ok", jd["ok"]) == {"state": "none"}, "a recording never split is 'none'")
    s = jobs.start("slow", "slow.mp3", jd["slow"])
    check(s["state"] in ("queued", "running"), "start queues a split", s["state"])
    check(wait_for(lambda: jobs.jobs["slow"]["events"] > 0), "the running job takes the child's progress")
    s = jobs.status("slow", jd["slow"])
    check(s["state"] == "running" and s["stage"] == "voice" and s["model_mb"] == 870 and s["cached"] is False
          and "src" not in s and "dir" not in s, "status shows the stage, model size and cache state", s)
    s = jobs.start("queued", "ok.mp3", jd["queued"])
    check(s["state"] == "queued" and s["ahead"] == 0, "a second split waits in the queue", s)
    jobs.cancel("queued")
    check(jobs.status("queued", jd["queued"])["state"] == "cancelled", "a queued split can be cancelled")
    tm = time.time()
    jobs.cancel("slow")
    check(wait_for(lambda: jobs.proc is None, 10) and time.time() - tm < 5, "cancel ends the running process")
    check(jobs.status("slow", jd["slow"])["state"] == "cancelled", "a cancelled split stays cancelled")
    check(wait_for(lambda: sorted(os.listdir(jd["slow"])) == ["split.log"], 10),     # _run cleans after the child ends
          "a cancelled split's work folder and .part files are removed", os.listdir(jd["slow"]))
    # Regression: a cancel that landed after the worker took the job but before the child started had no process
    # to end, and the child was started anyway. child_command is held here until the job has been cancelled.
    gate = threading.Event()

    def held(*args):
        gate.wait(10)
        return [sys.executable, fake_child, *args]
    stems.child_command = held
    jd["early"] = os.path.join(td, "jobs", "early")
    jobs.start("early", "ok.mp3", jd["early"])
    wait_for(lambda: jobs.jobs["early"]["state"] == "running", 10)
    jobs.cancel("early")
    stems.child_command = lambda *args: [sys.executable, fake_child, *args]
    gate.set()
    jobs.start("ok", "ok.mp3", jd["ok"])           # one worker: once this is done, "early" has been handled
    check(wait_for(lambda: jobs.jobs["ok"]["state"] == "done") and jobs.status("ok", jd["ok"]) == {"state": "done"},
          "a split that writes its parts is 'done'", jobs.jobs["ok"])
    check(jobs.jobs["ok"]["stage"] == "music" and jobs.jobs["ok"]["frac"] == 1.0,
          "the last progress event is kept (JSON lines that are not events are skipped)")
    check(sorted(os.listdir(jd["ok"])) == sorted(os.listdir(d) + ["split.log"]),
          "a finished split keeps its parts and loses its work folder and .part files", os.listdir(jd["ok"]))
    check(jobs.status("early", jd["early"])["state"] == "cancelled" and stems.read_manifest(jd["early"]) is None,
          "a split cancelled before its process started never starts it", os.listdir(jd["early"]))
    check(jobs.start("ok", "ok.mp3", jd["ok"]) == {"state": "done"} and jobs.queue == [],
          "starting a finished split again does nothing")
    jobs.start("fail", "fail.mp3", jd["fail"])
    check(wait_for(lambda: jobs.status("fail", jd["fail"])["state"] == "error"), "a failing child makes an 'error'")
    check(jobs.status("fail", jd["fail"]).get("error") == "RuntimeError: no audio", "the child's error is shown",
          jobs.status("fail", jd["fail"]).get("error"))
    s = jobs.start("fail", "slow.mp3", jd["fail"])
    check(s["state"] in ("queued", "running"), "a failed split can be started again", s["state"])
    wait_for(lambda: jobs.jobs["fail"]["events"] > 0)
    jobs.cancel("fail")
    wait_for(lambda: jobs.proc is None, 10)
    # The parts of a finished split can disappear while the server runs (server._prune_stems removes the least
    # recently used songs, or the user empties data/stems).
    os.remove(os.path.join(jd["ok"], "manifest.json"))
    s = jobs.status("ok", jd["ok"])
    # Regression: status() used to keep saying "done" from the old job record (so the API sent "done" without
    # "parts"), and start() re-queued only "error"/"cancelled" jobs, so such a recording could not be split again
    # until the server restarted.
    check(s["state"] == "none", "a split whose parts were removed is 'none' again", f"state {s['state']!r}")
    old = jobs.jobs["ok"]
    jobs.start("ok", "ok.mp3", jd["ok"])
    check(jobs.jobs["ok"] is not old and wait_for(lambda: stems.read_manifest(jd["ok"]) is not None),
          "a split whose parts were removed can be split again", "start() kept the old 'done' job")
finally:
    stems.child_command = real_child
section("split queue", "progress, queue, cancel before and after start, done, error, retry, clean up")

# ------------------------------------------------------------------ the child's signals
# Regression: Cancel ends the child with SIGTERM (a closed Terminal sends SIGHUP), which used to kill it at once
# and leave its split-* work folder (a copy of the recording per stage) behind. The child now turns both into
# SystemExit so split()'s `finally` removes it. A stand-in Separator reports the work folder and then hangs, so
# the signal always arrives inside split() with the folder there; no model is loaded.
sig_child = os.path.join(td, "sig_child.py")
with open(sig_child, "w") as f:
    f.write("""import json, sys, time, types
from bhajan import stems
class Separator:
    def __init__(self, output_dir, **kw):
        print(json.dumps({"work": output_dir}), flush=True)
        time.sleep(60)
for name in ("audio_separator", "audio_separator.separator", "soundfile"):
    sys.modules[name] = types.ModuleType(name)
sys.modules["audio_separator.separator"].Separator = Separator
stems._patch_separator = lambda emit: None
sys.exit(stems._main(sys.argv[1:]))
""")
for sig in (signal.SIGTERM, signal.SIGHUP):
    out = os.path.join(td, "sig", sig.name)
    p = subprocess.Popen([sys.executable, sig_child, "split", paths0["voice"], out, "--models", os.path.join(td, "models")],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=os.environ | {"PYTHONPATH": stems.ROOT})
    killer = threading.Timer(30, p.kill)          # never hang the suite if the child does not get that far
    killer.start()
    line = p.stdout.readline()
    work = json.loads(line)["work"] if line.startswith(b"{") else ""
    ready = os.path.basename(work).startswith("split-") and os.path.isdir(work)
    check(ready, f"{sig.name}: the stand-in split reaches its work folder", line)
    p.send_signal(sig)
    try:
        _, err = p.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()
        _, err = p.communicate()
    killer.cancel()
    check(p.returncode == 128 + sig, f"{sig.name}: the child exits with 128 + the signal",
          f"exit {p.returncode}: {err.decode(errors='replace').strip()[-300:]}")
    left = [n for n in os.listdir(out) if n.startswith("split-")] if os.path.isdir(out) else []
    check(ready and not left, f"{sig.name}: the child removes its split-* work folder", left)
section("child signals", "SIGTERM and SIGHUP end a split with its work folder removed")

# ------------------------------------------------------------------ model download
# Regression: audio-separator writes a model in place and skips any file already there, so a download that was
# cut short (or cancelled) used to be taken for the model on every later split. _patch_separator downloads into
# <file>.part, checks the size and renames it into place. A local server sends a short and a whole file.
try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")      # torch and pydub warn about their own code when imported
        stems._patch_separator(lambda e: None)       # imports audio-separator and torch (~5 s); loads no model
        from audio_separator.separator import Separator
except ImportError as e:
    Separator = None
    print(f"SKIP model download checks (they need the 'stems' extra: {e})")
if Separator:
    import http.server
    BODY = rng.integers(0, 256, 300_000, dtype=np.uint8).tobytes()

    class Models(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/missing.ckpt":
                self.send_error(404)
                return
            self.send_response(200)
            short = self.path == "/short.ckpt"      # promises 1000 bytes more than it sends, then hangs up
            self.send_header("Content-Length", str(len(BODY) + (1000 if short else 0)))
            self.end_headers()
            self.wfile.write(BODY)

        def log_message(self, *a):
            pass
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Models)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    md = os.path.join(td, "dl-models")
    os.makedirs(md)
    path = os.path.join(md, "model.ckpt")
    try:
        for what in ("short", "missing"):
            try:
                Separator.download_file_if_not_exists(None, f"{base}/{what}.ckpt", path)
                raised = None
            except Exception as e:
                raised = e
            check(raised is not None, f"a {what} download raises")
            # KNOWN BUG (bhajan/stems.py:213-227): requests raises ChunkedEncodingError from iter_content when the
            # server hangs up early (urllib3 2 enforces Content-Length), so the size check is never reached and
            # nothing removes model.ckpt.part (a cancelled split, SIGTERM -> SystemExit, leaves it too).
            check(os.listdir(md) == [], f"a {what} download leaves neither the model nor a .part file",
                  f"{os.listdir(md)}; KNOWN BUG in bhajan/stems.py _patch_separator.download, see the note above")
            for name in os.listdir(md):
                os.remove(os.path.join(md, name))
        Separator.download_file_if_not_exists(None, f"{base}/model.ckpt", path)
        with open(path, "rb") as f:
            check(f.read() == BODY and os.listdir(md) == ["model.ckpt"], "a whole download lands at its path, and only there",
                  os.listdir(md))
        Separator.download_file_if_not_exists(None, f"{base}/missing.ckpt", path)     # present: not fetched again
        check(os.path.getsize(path) == len(BODY), "a model already downloaded is kept")
    finally:
        srv.shutdown()
        srv.server_close()
    section("model download", "cut-short and missing downloads raise and leave nothing; a whole one lands atomically")

# ------------------------------------------------------------------ h) API routes
try:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")      # Starlette prefers httpx2 and warns about plain httpx
        from fastapi.testclient import TestClient
except (ImportError, RuntimeError) as e:
    TestClient = None
    print(f"SKIP API route checks (FastAPI's TestClient needs httpx2 or httpx, which is not installed: "
          f"{str(e).splitlines()[0]})")
if TestClient:
    os.environ["BHAJAN_DATA"] = os.path.join(td, "data")          # server.py reads it when imported
    import server
    from fastapi import HTTPException
    rec = os.path.join(td, "recording.flac")
    stems.write_flac(rec, mix[:, None], SR)

    def fake_recording(slug, i):
        if i > 1:
            raise HTTPException(404, "This bhajan has no such recording on Sai Rhythms.")
        return rec, f"https://example.invalid/{slug}/{i}.mp3"

    server._recording = fake_recording
    sm = fake_split(server._parts_dir("x", 0)[0], parts)               # recording 1 is not split
    c = TestClient(server.app, base_url="http://127.0.0.1")
    check(TestClient(server.app).get("/api/song/x/parts").status_code == 403, "requests to other hosts are refused")

    r = c.get("/api/song/x/parts")
    j = r.json()
    check(r.status_code == 200 and j["state"] == "done", "GET parts of a split recording is 'done'", j)
    check([p["id"] for p in j.get("parts", [])] == list(stems.PARTS)
          and all(p["label"] == stems.LABELS[p["id"]] and p["share_db"] == sm["parts"][p["id"]]["share_db"]
                  for p in j.get("parts", [])) and j.get("models") == sm["models"], "GET parts lists the parts", j)
    check(c.get("/api/song/x/parts?audio_i=1").json()["state"] == "none", "GET parts of an unsplit recording is 'none'")
    check(c.get("/api/song/x/parts?audio_i=5").status_code == 404, "GET parts of a missing recording is 404")
    r = c.post("/api/song/x/parts")
    check(r.status_code == 200 and r.json()["state"] == "done" and not server.splits.jobs,
          "POST parts of a split recording starts nothing")
    check(c.delete("/api/song/x/parts").json()["state"] == "done", "DELETE parts of a finished split changes nothing")

    r = c.post("/api/song/x/parts/render", json={"audio_i": 0, "semitones": 2, "tempo": 1.0})
    j = r.json()
    check(r.status_code == 200 and j["gain"] == 2.0 and j["sr"] == SR and sorted(j["urls"]) == sorted(stems.PARTS)
          and all(u.startswith("/stems/") for u in j["urls"].values()), "render returns the gain, rate and URLs", j)
    for p, u in j.get("urls", {}).items():
        g = c.get(u)
        cache = g.headers.get("cache-control", "")
        check(g.status_code == 200 and g.content[:4] == b"fLaC" and "no-cache" not in cache,
              f"GET {p} part URL serves a cacheable FLAC", (g.status_code, u, cache))
    # Regression: /stems/ once lacked /api/'s cross-site check, so any website could probe which bhajans were split.
    u = j.get("urls", {}).get("voice", "/stems/none")
    check(c.get(u, headers={"sec-fetch-site": "cross-site"}).status_code == 403
          and c.get(u, headers={"origin": "https://example.com"}).status_code == 403,
          "other websites may not fetch part URLs")
    check(c.post("/api/song/x/parts/render", json={"audio_i": 1}).status_code == 409,
          "render of an unsplit recording is 409")
    check(c.post("/api/song/x/parts/render", json={"semitones": 13}).status_code == 422,
          "render outside +-12.5 st is 422")

    def mix_mp3(gains):
        r = c.post("/api/song/x/parts/mix", json={"audio_i": 0, "semitones": 2, "tempo": 1.0, "gains": gains})
        if r.status_code != 200:
            return r, None
        g = c.get(r.json()["audio_url"])
        ok = g.status_code == 200 and (g.content[:3] == b"ID3" or g.content[:2] in (b"\xff\xfb", b"\xff\xf3"))
        path = os.path.join(server.MEDIA, os.path.basename(r.json()["audio_url"]))
        return r, stems.read_audio(path)[0] if ok else None

    r, y = mix_mp3({"tabla": None, "harmonium": -6})
    check(y is not None and abs(len(y) / SR - 4.0) <= 0.06, "mix renders a downloadable MP3 of the song's length",
          (r.status_code, r.text[:200], None if y is None else len(y) / SR))
    r, y = mix_mp3({p: 12 for p in stems.PARTS})
    check(y is not None and rms(y) <= 1.05 * sm["rms"] and np.abs(y).max() <= 1.0,
          "a mix with every part at +12 dB is no louder than the recording",
          None if y is None else f"rms {rms(y):.3f} vs {sm['rms']:.3f}, peak {np.abs(y).max():.3f}")
    check(mix_mp3({"flute": 0})[0].status_code == 422, "mix with an unknown part is 422")
    check(mix_mp3({p: None for p in stems.PARTS})[0].status_code == 422, "mix with every part muted is 422")

    body = {"name": "Thursday", "gap": 1.0,
            "items": [{"slug": "x", "title": "One", "semitones": 2, "gains": {"tabla": None}},
                      {"slug": "x", "title": "Two", "semitones": -3, "tempo": 0.9, "gains": {"voice": None}}]}
    with server._export_lock:            # another export is running: one at a time, so this one waits its turn
        r = c.post("/api/session/export", json=body)
        j = r.json()
        jid = j.get("id")
        q = c.get(f"/api/session/export/{jid}").json()
    check(r.status_code == 200 and j["state"] == "queued" and j["total"] == 2 and q.get("state") == "queued",
          "a session export waits while another runs", (j, q))
    # Regression: POST used to return the job dict as is, with "path" (the MP3's absolute path in data/media),
    # which GET /api/session/export/{id} leaves out. No response may carry it (checked on each one below).
    check("path" not in j and "path" not in q, "POST and GET session export do not expose the local file path", (j, q))

    def export_ended():
        return c.get(f"/api/session/export/{jid}").json()["state"] not in ("queued", "running")
    check(wait_for(export_ended, 120), "session export finishes")
    j = c.get(f"/api/session/export/{jid}").json()
    check(j["state"] == "done" and [tr["title"] for tr in j.get("tracks", [])] == ["One", "Two"] and "path" not in j,
          "a finished export lists its tracks", j)
    url = j.get("audio_url", "/media/none")
    check(url.startswith("/media/") and c.get(url).status_code == 200, "the session MP3 can be downloaded")
    j = c.post("/api/session/export", json=body).json()
    check(j.get("state") == "done" and j.get("id") == jid and "path" not in j, "the same session again is ready at once", j)
    # data/media pruning can remove the MP3 of a finished export: GET then says so instead of a dead audio_url,
    # and POST renders the session again.
    os.remove(os.path.join(server.MEDIA, os.path.basename(url)))
    j = c.get(f"/api/session/export/{jid}").json()
    check(j.get("state") == "expired" and "audio_url" not in j and "path" not in j,
          "an export whose MP3 was removed is 'expired'", j)
    j = c.post("/api/session/export", json=body).json()
    check(j.get("state") in ("queued", "running") and "path" not in j, "an expired session is exported again", j)
    check(wait_for(export_ended, 120) and c.get(url).status_code == 200, "the session MP3 is back")
    body["items"][1]["audio_i"] = 1
    check(c.post("/api/session/export", json=body).status_code == 409, "a session with an unsplit recording is 409")
    check(c.get("/api/session/export/nope").status_code == 404, "an unknown export id is 404")

    # data/stems is kept under a cap: shifted copies (cheap to redo) go before whole splits (a GPU split to redo),
    # least recently used first (mtimes), and a split that is queued or running, or anything used in the last
    # 10 minutes, is kept. Three old recordings are added next to "x" (used just now):
    #   p1  being split again (busy), its folder last touched 5000 s ago
    #   p2  split 4000 s ago, shifted sets used 3000 s and 2400 s ago
    #   p3  split 3500 s ago, a shifted set used 1800 s ago
    wait_for(lambda: not any(th.name == "session-export" for th in threading.enumerate()), 60)   # these prune too

    def tree(p):
        return sum(os.path.getsize(os.path.join(r_, f)) for r_, _, fs in os.walk(p) for f in fs)

    def stems_bytes():
        return sum(tree(os.path.join(server.STEMS, n)) for n in os.listdir(server.STEMS)
                   if os.path.isdir(os.path.join(server.STEMS, n)))

    def ago(p, s):
        os.utime(p, (time.time() - s, time.time() - s))
    shift_set = os.path.join(d, "shift", stems.shift_key(-3.0, 1.0))        # a real one, copied
    pd, pk, sh = {}, {}, {}
    for slug in ("p1", "p2", "p3"):
        pd[slug], _, pk[slug] = server._parts_dir(slug, 0)
    os.makedirs(os.path.join(pd["p1"], "split-w1"))
    shutil.copy(rec, os.path.join(pd["p1"], "split-w1", "voice-in.flac"))
    open(os.path.join(pd["p1"], "split.log"), "w").close()
    ago(pd["p1"], 5000)
    for slug, age, shifts in (("p2", 4000, {"-1": 3000, "-2": 2400}), ("p3", 3500, {"-1": 1800})):
        shutil.copytree(d, pd[slug], ignore=shutil.ignore_patterns("shift"))
        for k, a in shifts.items():
            sh[slug + k] = os.path.join(pd[slug], "shift", stems.shift_key(float(k), 1.0))
            shutil.copytree(shift_set, sh[slug + k])
            ago(sh[slug + k], a)
        ago(os.path.join(pd[slug], "manifest.json"), age)
    with server.splits.cv:
        server.splits.jobs[pk["p1"]] = {"state": "running", "src": rec, "dir": pd["p1"], "events": 0}
        server.splits.jobs[pk["p2"]] = {"state": "done", "src": rec, "dir": pd["p2"], "events": 0}
    xd = server._parts_dir("x", 0)[0]
    x_files = sorted(r_ for r_, _, _ in os.walk(xd))
    check(server.splits.busy() == {pk["p1"]} and c.get("/api/song/p2/parts").json()["state"] == "done",
          "before pruning: p1 is busy and p2 is 'done'")
    try:
        server._prune_stems(cap=stems_bytes() - tree(sh["p2-1"]))
        check(not os.path.exists(sh["p2-1"]) and all(os.path.exists(sh[k]) for k in ("p2-2", "p3-1"))
              and all(os.path.isdir(p) for p in pd.values()),
              "pruning removes the least recently used shifted set first, before any (older) recording")
        server._prune_stems(cap=stems_bytes() - tree(sh["p2-2"]) - tree(sh["p3-1"]) - 1)
        check(not any(os.path.exists(p) for p in sh.values()) and not os.path.exists(pd["p2"])
              and os.path.isdir(pd["p1"]) and os.path.isdir(pd["p3"]),
              "then every old shifted set, then the least recently used recording that is not being split",
              sorted(n for n in os.listdir(server.STEMS) if os.path.isdir(os.path.join(server.STEMS, n))))
        check(pk["p2"] not in server.splits.jobs and c.get("/api/song/p2/parts").json()["state"] == "none",
              "a recording removed by pruning is forgotten and reports 'none'")
        server._prune_stems(cap=0)
        check(not os.path.exists(pd["p3"]) and os.path.isdir(os.path.join(pd["p1"], "split-w1"))
              and sorted(r_ for r_, _, _ in os.walk(xd)) == x_files and stems.read_manifest(xd) is not None,
              "even at cap 0, a split in progress and a recording used in the last 10 minutes are kept")
    finally:
        with server.splits.cv:
            server.splits.jobs.pop(pk["p1"], None)

    # Sai Rhythms tags: "golden voice" recordings (Swami's own voice) are never split; songs tagged "karaoke" or
    # "instrumental" say so, because their official accompaniment recording beats any split.
    real_tags = server._song_tags
    try:
        server._song_tags = lambda slug: {"golden voice"}
        j = c.get("/api/song/x/parts").json()
        check(j.get("state") == "unavailable" and "Swami" in j.get("msg", ""), "a golden-voice recording is 'unavailable'", j)
        r = c.post("/api/song/x/parts?audio_i=1")
        check(r.status_code == 403 and not server.splits.jobs, "POST parts of a golden-voice recording is 403 and starts nothing",
              (r.status_code, r.text[:120]))
        server._song_tags = lambda slug: {"karaoke", "vintage"}
        j = c.get("/api/song/x/parts").json()
        check(j.get("state") == "done" and j.get("official") == ["karaoke"], "a karaoke-tagged song points to its official track", j)
    finally:
        server._song_tags = real_tags

    # A newer export can cancel one the page abandoned: DELETE ends it before its next bhajan (or before it starts).
    body2 = {"name": "Cancel me", "gap": 0.5, "items": [{"slug": "x", "title": "C1", "semitones": 1},
                                                        {"slug": "x", "title": "C2", "semitones": -1}]}
    with server._export_lock:
        jid2 = c.post("/api/session/export", json=body2).json()["id"]
        j = c.delete(f"/api/session/export/{jid2}").json()
    check(j.get("state") == "queued" and "cancel" not in j and "path" not in j, "DELETE marks a queued export to stop", j)
    check(wait_for(lambda: c.get(f"/api/session/export/{jid2}").json()["state"] not in ("queued", "running"), 60)
          and c.get(f"/api/session/export/{jid2}").json()["state"] == "cancelled", "a cancelled export ends as 'cancelled'")
    check(not server._exporting, "a finished or cancelled export no longer pins its recordings against pruning",
          server._exporting)
    check(c.delete("/api/session/export/nope").status_code == 404, "DELETE of an unknown export is 404")
    section("API", "parts status, render, part URLs, mix, session export (queued, expired, cancel), pruning data/stems, "
                   "golden voice, official tracks")

TMP.cleanup()
if failed:
    print(f"{len(failed)} STEMS CHECK(S) FAILED:\n  " + "\n  ".join(failed))
    sys.exit(1)
print("ALL STEMS TESTS PASSED")
