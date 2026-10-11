"""Split a bhajan recording into parts, then remix the parts in any key.

Parts: the singing (the lead and the group that answers each line, together), tabla (with
manjira and other percussion), harmonium (with the other melody instruments), and the remainder. Every part is
cut from the same recording, so with all parts at 0 dB the mix is the original recording.

Splitting runs open music-separation models (Mel-Band / BS-RoFormer checkpoints) through the
audio-separator package on PyTorch. They are an optional extra, installed the first time a
recording is split (`uv run --extra stems`), and they run in a child process,
`python -m bhajan.stems split SRC DIR`, so the server never loads PyTorch and a split can be
cancelled by ending the process. The child prints one JSON object per line: progress, then the
manifest (or an error).

Remixing needs only NumPy and Rubber Band: each part is shifted (and optionally slowed down or
sped up) once per key and cached, the browser mixes the cached parts live, and exports are
rendered here with the same peak protection as the whole-recording shift.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from typing import Callable

import numpy as np
import scipy.signal as ss

from bhajan import audio, store
from bhajan.media import ffmpeg_exe

PARTS = ("voice", "tabla", "harmonium", "other")
LABELS = {"voice": "Singing (lead and group)", "tabla": "Tabla & percussion",
          "harmonium": "Harmonium & melody instruments", "other": "Everything else"}

# Change STEMS_VERSION whenever the cascade or its post-processing changes, so recordings are re-split.
STEMS_VERSION = "cascade-3"
SHIFT_VERSION = "stems-r3-joint-1"
MODEL_SR = 44100
HEADROOM = 0.5            # the models get the recording at -6 dB, so no part can clip
# Each stage runs one model on the output of an earlier stage. `take` maps the model's stem
# names to ours; anything a model returns that is not mapped (and the stage's rounding
# residue) goes to `rest`, so the parts always add up to the stage's input.
CASCADE = (
    {"stage": "voice", "msg": "Separating the singing from the instruments",
     "model": "vocals_mel_band_roformer.ckpt", "input": "mix",
     "take": {"vocals": "voice"}, "rest": "music"},
    {"stage": "music", "msg": "Separating tabla from harmonium",
     "model": "BS-Roformer-SW.ckpt", "input": "music",
     "take": {"drums": "tabla"}, "rest": "harmonium", "refine": "percussive"},
)
MODEL_MB = {"vocals_mel_band_roformer.ckpt": 870,
            "BS-Roformer-SW.ckpt": 700}
MDXC_OVERLAP = 2          # prediction windows per chunk: 2 hides chunk seams at half the cost of 4


# ------------------------------------------------------------------ helpers
def decode(src: str, sr: int, channels: int) -> np.ndarray:
    """Whole file -> (frames, channels) float32, via the bundled ffmpeg."""
    blocks = [b for b, _ in audio._decoded_blocks(src, sr=sr, channels=channels)]
    if not blocks:
        raise RuntimeError(f"no audio could be decoded from {os.path.basename(src)}")
    return np.ascontiguousarray(np.concatenate(blocks, axis=1).T)


def probe(src: str) -> tuple[int, int]:
    """(sample rate, channels) of the first audio stream, from ffmpeg's banner."""
    r = subprocess.run([ffmpeg_exe(), "-hide_banner", "-nostdin", "-i", src], capture_output=True, text=True)
    for line in r.stderr.splitlines():
        if "Audio:" in line:
            parts = [p.strip() for p in line.split("Audio:", 1)[1].split(",")]
            sr = next((int(p.split()[0]) for p in parts if p.endswith("Hz")), MODEL_SR)
            ch = 1 if any(p.startswith("mono") or p.startswith("1 channels") for p in parts) else 2
            return sr, ch
    raise RuntimeError(f"no audio stream in {os.path.basename(src)}")


def resample(x: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    if sr_from == sr_to:
        return x
    g = math.gcd(sr_from, sr_to)
    return ss.resample_poly(x, sr_to // g, sr_from // g, axis=0).astype(np.float32)


def write_flac(path: str, x: np.ndarray, sr: int) -> None:
    """(frames, ch) float32 -> 24-bit FLAC, written atomically."""
    def _w(tmp: str) -> None:
        r = subprocess.run([ffmpeg_exe(), "-v", "error", "-y", "-f", "f32le", "-ar", str(sr), "-ac", str(x.shape[1]),
                            "-i", "pipe:0", "-c:a", "flac", "-sample_fmt", "s32", "-bits_per_raw_sample", "24",
                            "-f", "flac", tmp], input=np.ascontiguousarray(x, np.float32).tobytes(), capture_output=True)
        if r.returncode:
            raise RuntimeError("ffmpeg could not write FLAC: " + r.stderr.decode(errors="replace").strip()[-300:])
    store.write_with(path, _w)


def read_audio(path: str) -> tuple[np.ndarray, int]:
    sr, ch = probe(path)
    return decode(path, sr, ch), sr


# ------------------------------------------------------------------ loudness (ITU-R BS.1770-5)
def _k_weighting(sr: int):
    """K-weighting as (b, a) pairs at any rate, designed as libebur128 does: at 48 kHz this gives
    exactly the coefficients tabulated in BS.1770."""
    k = np.tan(np.pi * 1681.974450955533 / sr)
    q = 0.7071752369554196
    vh = 10 ** (3.999843853973347 / 20)
    vb = vh ** 0.4996667741545416
    a0 = 1 + k / q + k * k
    shelf = (np.array([vh + vb * k / q + k * k, 2 * (k * k - vh), vh - vb * k / q + k * k]) / a0,
             np.array([1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0]))
    k = np.tan(np.pi * 38.13547087602444 / sr)
    q = 0.5003270373238773
    a0 = 1 + k / q + k * k
    highpass = (np.array([1.0, -2.0, 1.0]), np.array([1.0, 2 * (k * k - 1) / a0, (1 - k / q + k * k) / a0]))
    return shelf, highpass


def loudness(x: np.ndarray, sr: int) -> float:
    """Integrated loudness in LUFS of (frames, ch) audio: K-weighting, 400 ms blocks with 75 %
    overlap, -70 LUFS absolute and -10 LU relative gates. Channels are weighted 1 (mono, L, R)."""
    blk, hop = int(0.4 * sr), int(0.1 * sr)
    if len(x) < blk:
        return -70.0
    starts = np.arange(0, len(x) - blk + 1, hop)
    z = np.zeros(len(starts))
    for c in range(x.shape[1]):                  # one channel at a time keeps memory to ~2 copies of it
        y = x[:, c].astype(np.float64)
        for b, a in _k_weighting(sr):
            y = ss.lfilter(b, a, y)
        cs = np.concatenate([[0.0], np.cumsum(y * y)])
        z += (cs[starts + blk] - cs[starts]) / blk
    with np.errstate(divide="ignore"):
        lk = -0.691 + 10 * np.log10(z)
    z = z[lk > -70]
    if not len(z):
        return -70.0
    rel = -0.691 + 10 * np.log10(z.mean()) - 10
    z = z[-0.691 + 10 * np.log10(z) > rel]
    return float(-0.691 + 10 * np.log10(z.mean())) if len(z) else -70.0


# ------------------------------------------------------------------ splitting (child process)
def _patch_separator(emit: Callable[[dict], None]) -> None:
    """Make audio-separator report progress to us, download models safely, and keep its output
    exactly as separated."""
    import importlib

    from audio_separator.separator import separator as sep_mod
    from audio_separator.separator.uvr_lib_v5 import spec_utils
    spec_utils.normalize = lambda wave, max_peak=1.0, min_peak=None: wave   # no per-stem rescaling

    class _Progress:
        """Stands in for tqdm: iterates (or counts download bytes) and reports a fraction."""
        def __init__(self, iterable=None, total=None, **_):
            self.it, self.n = iterable, 0
            self.total = total if total is not None else (len(iterable) if hasattr(iterable, "__len__") else None)
            self.download = iterable is None
            self._last = -1.0

        def _report(self):
            if self.total:
                f = min(1.0, self.n / self.total)
                if f - self._last >= 0.02 or f >= 1.0:
                    self._last = f
                    emit({"sub": "download" if self.download else "separate", "frac": round(f, 3)})

        def __iter__(self):
            for x in self.it:
                yield x
                self.n += 1
                self._report()

        def update(self, k=1):
            self.n += k
            self._report()

        def close(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    for mod in ("audio_separator.separator.separator",
                "audio_separator.separator.architectures.mdxc_separator",
                "audio_separator.separator.architectures.mdx_separator",
                "audio_separator.separator.architectures.vr_separator"):
        try:
            setattr(importlib.import_module(mod), "tqdm", _Progress)
        except Exception:
            pass

    def download(self, url, output_path):
        """audio-separator's download, but into a .part file renamed only once complete: its own
        writes in place and skips any existing file, so a cancelled or dropped download would
        otherwise be taken for the model on every later split."""
        if os.path.isfile(output_path):
            return
        import requests
        tmp = output_path + ".part"
        try:
            with requests.get(url, stream=True, timeout=300) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"could not download {os.path.basename(output_path)} (HTTP {r.status_code})")
                total = int(r.headers.get("content-length", 0)) if not r.headers.get("content-encoding") else 0
                bar, got = _Progress(total=total or None), 0
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=1 << 20):
                        f.write(chunk)
                        got += len(chunk)
                        bar.update(len(chunk))
            if total and got != total:
                raise RuntimeError(f"the download of {os.path.basename(output_path)} was cut short ({got} of {total} bytes)")
            os.replace(tmp, output_path)
        finally:                                  # a dropped connection, an error or Cancel (SystemExit)
            if os.path.exists(tmp):
                os.remove(tmp)
    sep_mod.Separator.download_file_if_not_exists = download


def _forget_model(models_dir: str, model: str) -> None:
    """Delete a model's files so it is downloaded again (e.g. one damaged by an older, unsafe download)."""
    stem = os.path.splitext(model)[0]
    for name in os.listdir(models_dir):
        if name.startswith(stem):
            os.remove(os.path.join(models_dir, name))


def _clean_work(d: str) -> None:
    """Remove what an interrupted split leaves in its folder: work folders and half-written files."""
    if not os.path.isdir(d):
        return
    for name in os.listdir(d):
        p = os.path.join(d, name)
        if name.startswith("split-") and os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        elif name.startswith(".") and ".part" in name:
            try:
                os.remove(p)
            except OSError:
                pass


def split(src: str, out_dir: str, models_dir: str, emit: Callable[[dict], None] = lambda e: None) -> dict:
    """Separate `src` into PARTS (FLAC files in out_dir) and write out_dir/manifest.json."""
    import logging

    import soundfile as sf
    _patch_separator(emit)
    from audio_separator.separator import Separator

    t_start = time.time()
    os.makedirs(out_dir, exist_ok=True)
    os.makedirs(models_dir, exist_ok=True)
    _clean_work(out_dir)
    src_sr, src_ch = probe(src)
    part_sr = min(src_sr, MODEL_SR)
    mix = decode(src, MODEL_SR, src_ch)
    if src_ch == 1:   # duplicate by hand: ffmpeg's "-ac 2" upmix puts mono at -3 dB in each channel
        mix = np.repeat(mix, 2, axis=1)
    mix *= np.float32(HEADROOM)
    n = len(mix)
    work = tempfile.mkdtemp(prefix="split-", dir=out_dir)
    try:
        signals = {"mix": mix}
        sep = Separator(output_dir=work, model_file_dir=models_dir, output_format="WAV",
                        log_level=logging.WARNING, use_soundfile=True, normalization_threshold=1.0,
                        mdxc_params={"segment_size": 256, "override_model_segment_size": False,
                                     "batch_size": None, "overlap": MDXC_OVERLAP, "pitch_shift": 0})
        for i, st in enumerate(CASCADE):
            base = {"stage": st["stage"], "step": i + 1, "steps": len(CASCADE), "msg": st["msg"]}
            emit(base | {"sub": "load", "frac": 0.0,
                         "model_mb": MODEL_MB.get(st["model"]),
                         "cached": os.path.exists(os.path.join(models_dir, st["model"]))})
            try:
                sep.load_model(model_filename=st["model"])
            except Exception:
                # A checkpoint that is not a readable archive was damaged (e.g. by an older, unsafe
                # download): fetch it again, once. Anything else (a network error, ...) is reported.
                ckpt = os.path.join(models_dir, st["model"])
                if not (ckpt.endswith(".ckpt") and os.path.isfile(ckpt) and not zipfile.is_zipfile(ckpt)):
                    raise
                _forget_model(models_dir, st["model"])
                sep.load_model(model_filename=st["model"])
            x = signals[st["input"]]
            inp = os.path.join(work, f"{st['stage']}-in.wav")
            sf.write(inp, x, MODEL_SR, subtype="FLOAT")             # float in -> float out, nothing clips
            emit(base | {"sub": "separate", "frac": 0.0})
            outs = sep.separate(inp)
            got = {}
            for f in outs:
                name = f.rsplit("(", 1)[-1].split(")", 1)[0].strip().lower()
                y, _ = sf.read(os.path.join(work, os.path.basename(f)), dtype="float32", always_2d=True)
                got[name] = _fit(y, n)
            taken = np.zeros_like(x)
            for name, ours in st["take"].items():
                if name not in got:
                    raise RuntimeError(f"{st['model']} returned {sorted(got)}, not '{name}'")
                signals[ours] = got[name]
                taken += got[name]
            signals[st["rest"]] = x - taken                        # exact remainder: parts add up
            if st.get("refine") == "percussive":
                emit(base | {"sub": "refine", "frac": 0.0})
                ours = next(iter(st["take"].values()))
                if src_ch == 1:      # both channels are the same mono signal: refine one, copy it
                    p = np.repeat(percussive(signals[ours][:, :1], MODEL_SR), 2, axis=1)
                else:
                    p = percussive(signals[ours], MODEL_SR)
                signals[st["rest"]] += signals[ours] - p
                signals[ours] = p
            for f in os.listdir(work):
                os.remove(os.path.join(work, f))
        parts = {p: signals[p] for p in PARTS if p in signals}
        parts["other"] = mix - sum(parts.values())                 # anything left (normally ~silence)
        mix_e = float((mix.astype(np.float64) ** 2).sum()) or 1.0
        shares = {p: float((parts[p].astype(np.float64) ** 2).sum()) / mix_e for p in PARTS}
        out = {}
        for p in PARTS:
            y = parts.pop(p)
            if src_ch == 1:
                y = y.mean(axis=1, keepdims=True)
            out[p] = resample(y, MODEL_SR, part_sr)
        # A separated part can peak above the mix (limited masters), so if any would clip, every part
        # is turned down by the same factor and the manifest's gain makes up for it: they still add up.
        peak = max(float(np.abs(y).max(initial=0)) for y in out.values())
        k = 0.999 / peak if peak >= 0.999 else 1.0
        manifest = {"version": STEMS_VERSION, "sr": part_sr, "channels": src_ch, "frames": len(out["voice"]),
                    "gain": 1 / (HEADROOM * k), "rms": float(np.sqrt(np.mean(mix.astype(np.float64) ** 2))) / HEADROOM,
                    "parts": {}, "models": [s["model"] for s in CASCADE],
                    "seconds": round(time.time() - t_start, 1)}
        for p in PARTS:
            write_flac(os.path.join(out_dir, p + ".flac"), out[p] * np.float32(k) if k < 1 else out[p], part_sr)
            manifest["parts"][p] = {"file": p + ".flac", "share_db": round(10 * math.log10(shares[p] + 1e-12), 1)}
        store.write_json(os.path.join(out_dir, "manifest.json"), manifest)
        return manifest
    finally:
        shutil.rmtree(work, ignore_errors=True)


def percussive(x: np.ndarray, sr: int) -> np.ndarray:
    """The percussive part of (frames, ch) audio, by median-filter HPSS with soft masks.

    The drums stem of a pop-trained model also takes sustained sound from bhajans: the harmonium's
    notes and drones such as a shruti box. Tones held for longer than about a second are taken out
    (they go back to the harmonium part); strokes, with their short ring, stay. The dayan's longest
    ring partly goes with them, which is the price of a clean tabla part.
    """
    n_fft, hop = 4096, 1024
    kh = int(round(1.1 * sr / hop)) | 1          # ~1.1 s along time: harmonic
    kp = int(round(400 * n_fft / sr)) | 1        # ~400 Hz along frequency: percussive
    out = np.empty_like(x)
    for c in range(x.shape[1]):
        _, _, z = ss.stft(x[:, c], nperseg=n_fft, noverlap=n_fft - hop)
        mag = np.abs(z).astype(np.float32)
        h = ss.medfilt2d(mag, (1, kh))
        pm = ss.medfilt2d(mag, (kp, 1))
        mask = pm ** 2 / (h ** 2 + pm ** 2 + 1e-12)
        _, y = ss.istft(z * mask, nperseg=n_fft, noverlap=n_fft - hop)
        out[:, c] = y[:len(x)] if len(y) >= len(x) else np.pad(y, (0, len(x) - len(y)))
    return out


def _fit(y: np.ndarray, n: int) -> np.ndarray:
    """(frames, 2) of exactly n frames: mono doubled, length padded or trimmed."""
    if y.shape[1] == 1:
        y = np.repeat(y, 2, axis=1)
    y = y[:n, :2]
    if len(y) < n:
        y = np.pad(y, ((0, n - len(y)), (0, 0)))
    return y.astype(np.float32)


def read_manifest(d: str) -> dict | None:
    """The split's manifest, or None unless it is current and every part file is present."""
    m = store.read_json(os.path.join(d, "manifest.json"))
    try:
        if not m or m.get("version") != STEMS_VERSION:
            return None
        if not all(os.path.exists(os.path.join(d, m["parts"][p]["file"])) for p in PARTS):
            return None
    except (KeyError, TypeError, AttributeError):
        return None
    return m


def touch(d: str) -> None:
    """Mark a recording's parts as just used (pruning removes the least recently used first)."""
    try:
        os.utime(os.path.join(d, "manifest.json"))
    except OSError:
        pass


# ------------------------------------------------------------------ split jobs (server side)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STEMS_PACKAGES = ("audio_separator", "torch", "onnxruntime", "soundfile")


def _uv() -> str | None:
    exe = os.environ.get("UV") or shutil.which("uv")
    if not exe:
        home = os.path.expanduser("~/.local/bin/uv")
        exe = home if os.path.exists(home) else None
    return exe


def child_command(*args: str) -> list[str]:
    """How to run `python -m bhajan.stems ARGS` with the separation packages available: directly
    when they are all installed, otherwise through uv, which installs the 'stems' extra first."""
    try:
        import importlib.util
        if all(importlib.util.find_spec(m) is not None for m in STEMS_PACKAGES):
            return [sys.executable, "-m", "bhajan.stems", *args]
    except Exception:
        pass
    uv = _uv()
    if not uv:
        raise RuntimeError("Splitting needs the 'stems' extra: run `uv sync --extra stems` in the Bhajan Studio folder.")
    return [uv, "run", "--project", ROOT, "--frozen", "--extra", "stems", "--python", "3.12",
            "python", "-m", "bhajan.stems", *args]


def _stop(proc: subprocess.Popen, grace: float = 10.0) -> None:
    """SIGTERM the child's whole process group (uv, python, ffmpeg) so it can clean up; SIGKILL it
    if it is still there after `grace` seconds (e.g. stuck in one long GPU call)."""
    import signal

    def send(sig):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass
    send(signal.SIGTERM)

    def later():
        if proc.poll() is None:
            send(signal.SIGKILL)
    t = threading.Timer(grace, later)
    t.daemon = True
    t.start()


class SplitJobs:
    """One split at a time (the models want the whole GPU); later requests wait in a queue."""

    def __init__(self, models_dir: str):
        self.models_dir = models_dir
        self.jobs: dict[str, dict] = {}
        self.queue: list[str] = []
        self.cv = threading.Condition()
        self.proc: subprocess.Popen | None = None
        threading.Thread(target=self._worker, daemon=True, name="stems-split").start()

    def status(self, key: str, out_dir: str) -> dict:
        if read_manifest(out_dir):
            return {"state": "done"}
        with self.cv:
            j = self.jobs.get(key)
            if j is None or j["state"] == "done":     # never split, or its parts have since been pruned
                return {"state": "none"}
            s = {k: v for k, v in j.items() if k not in ("src", "dir")}
            if s["state"] == "queued":
                s["ahead"] = self.queue.index(key)
            return s

    def start(self, key: str, src: str, out_dir: str) -> dict:
        with self.cv:
            j = self.jobs.get(key)
            if not read_manifest(out_dir) and (j is None or j["state"] not in ("queued", "running")):
                self.jobs[key] = {"state": "queued", "src": src, "dir": out_dir, "events": 0,
                                  "stage": None, "step": 0, "steps": len(CASCADE), "frac": 0.0,
                                  "msg": "Waiting for another split to finish", "started": None}
                self.queue.append(key)
                self.cv.notify()
        return self.status(key, out_dir)

    def busy(self) -> set[str]:
        """Keys of the splits that are queued or running (their folders must not be pruned)."""
        with self.cv:
            return {k for k, j in self.jobs.items() if j["state"] in ("queued", "running")}

    def forget(self, key: str) -> None:
        """Drop a finished job's record (its folder was pruned)."""
        with self.cv:
            if key in self.jobs and self.jobs[key]["state"] not in ("queued", "running"):
                del self.jobs[key]

    def cancel(self, key: str) -> None:
        with self.cv:
            j = self.jobs.get(key)
            if not j:
                return
            if j["state"] == "queued":
                self.queue.remove(key)
                j["state"] = "cancelled"
            elif j["state"] == "running":
                j["state"] = "cancelled"          # _run also checks this before starting the child
                if self.proc:
                    _stop(self.proc)

    def _worker(self) -> None:
        while True:
            with self.cv:
                while not self.queue:
                    self.cv.wait()
                key = self.queue.pop(0)
                j = self.jobs[key]
                j.update(state="running", started=time.time(), msg="Starting (the first split installs PyTorch, about 1 GB)")
            try:
                self._run(j)
            except Exception as e:
                with self.cv:
                    if j["state"] != "cancelled":
                        j.update(state="error", error=str(e))

    def _run(self, j: dict) -> None:
        os.makedirs(j["dir"], exist_ok=True)
        cmd = child_command("split", j["src"], j["dir"], "--models", self.models_dir)
        err, rc = None, None
        with open(os.path.join(j["dir"], "split.log"), "ab") as log:
            with self.cv:
                if j["state"] == "cancelled":
                    return
                env = os.environ | {"PYTHONPATH": ROOT}       # find bhajan/ whatever the server's cwd
                proc = self.proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                                    stderr=log, stdin=subprocess.DEVNULL, start_new_session=True)
            try:
                for line in proc.stdout:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(ev, dict):
                        continue
                    with self.cv:
                        if "error" in ev:
                            err = str(ev["error"])
                        elif "manifest" not in ev:
                            j.update({k: ev[k] for k in ("stage", "step", "steps", "msg", "sub", "frac", "model_mb", "cached") if k in ev})
                            j["events"] += 1
                rc = proc.wait()
            finally:
                if proc.poll() is None:            # stopped reading early: never leave a split running
                    _stop(proc)
                    proc.wait()
                with self.cv:
                    self.proc = None
                _clean_work(j["dir"])
        with self.cv:
            if j["state"] == "cancelled":
                return
            if rc == 0 and read_manifest(j["dir"]):
                j.update(state="done", frac=1.0)
                return
            if err and ("ModuleNotFoundError" in err or "ImportError" in err):
                err += " (the separation packages look incomplete: run `uv sync --extra stems` in the Bhajan Studio folder)"
            tail = ""
            try:
                with open(os.path.join(j["dir"], "split.log"), "rb") as f:
                    f.seek(max(0, os.path.getsize(f.name) - 600))
                    lines = f.read().decode(errors="replace").strip().splitlines()
                    tail = lines[-1] if lines else ""
            except OSError:
                pass
            j.update(state="error", error=err or tail or f"the separation process exited with code {rc}")


# ------------------------------------------------------------------ per-part shift (cached)
SHIFT_SLOTS = threading.BoundedSemaphore(2)   # shift sets rendered at once: each holds a recording's parts in memory


def shift_key(semitones: float, tempo: float) -> str:
    return f"{semitones:+.3f}st_{tempo:.3f}x_{SHIFT_VERSION}"


def shift_array(x: np.ndarray, sr: int, semitones: float, tempo: float, part: str | None = None) -> np.ndarray:
    """(frames, ch) -> shifted (frames', ch) with Rubber Band R3 offline, formants preserved (as in
    the whole-recording shift). tempo > 1 = faster. Channels are processed together, so passing
    every part as channels of one array keeps the bleed they share in phase (see shifted_parts)."""
    if abs(semitones) < 1e-6 and abs(tempo - 1) < 1e-6:
        return x
    import pylibrb
    O = pylibrb.Option
    ch = x.shape[1]
    st = pylibrb.RubberBandStretcher(sr, ch, O.PROCESS_OFFLINE | O.ENGINE_FINER | O.CHANNELS_TOGETHER | O.FORMANT_PRESERVED,
                                     1.0 / tempo, 2.0 ** (semitones / 12.0))
    st.set_max_process_size(audio._RB_BLOCK)
    xt = np.ascontiguousarray(x.T)
    n = xt.shape[1]
    st.set_expected_input_duration(n)
    for i in range(0, n, audio._RB_BLOCK):
        st.study(np.ascontiguousarray(xt[:, i:i + audio._RB_BLOCK]), i + audio._RB_BLOCK >= n)
    out = np.zeros((int(round(n / tempo)) + 4 * audio._RB_BLOCK, ch), np.float32)
    pos = 0
    for i in range(0, n, audio._RB_BLOCK):
        st.process(np.ascontiguousarray(xt[:, i:i + audio._RB_BLOCK]), i + audio._RB_BLOCK >= n)
        pos = audio._drain(st, out, pos)
    pos = audio._drain(st, out, pos)
    return out[:pos]


def shifted_parts(d: str, semitones: float, tempo: float = 1.0) -> tuple[dict[str, str], float]:
    """({part: FLAC path}, gain) for every part shifted by `semitones` at `tempo`, rendered once and
    cached. Multiply the parts by `gain` to hear them at the original recording's level."""
    m = read_manifest(d)
    if not m:
        raise RuntimeError("This recording has not been split yet.")
    touch(d)
    if abs(semitones) < 1e-6 and abs(tempo - 1) < 1e-6:
        return {p: os.path.join(d, m["parts"][p]["file"]) for p in PARTS}, m["gain"]
    sd = os.path.join(d, "shift", shift_key(semitones, tempo))
    paths = {p: os.path.join(sd, p + ".flac") for p in PARTS}
    info = os.path.join(sd, "shift.json")
    with store.lock(sd):
        meta = store.read_json(info)
        if meta and all(os.path.exists(p) for p in paths.values()):
            os.utime(sd)                                     # pruning removes least recently used keys first
            return paths, meta["gain"]
        with SHIFT_SLOTS:
            # Every part goes through ONE stretcher, as channels: the parts share bleed (a mask-based
            # split puts some of each sound in two parts), and separate stretchers would give those
            # copies unrelated phases, so they no longer add back up (measured: -0.7 dB and a
            # phasier sum; together: -0.2 dB).
            xs = [read_audio(os.path.join(d, m["parts"][p]["file"])) for p in PARTS]
            sr, ch = xs[0][1], xs[0][0].shape[1]
            n = max(len(x) for x, _ in xs)
            stack = np.zeros((n, ch * len(PARTS)), np.float32)
            for i, (x, _) in enumerate(xs):
                stack[:len(x), i * ch:(i + 1) * ch] = x
            del xs
            y = shift_array(stack, sr, semitones, tempo)
            del stack
            # A phase vocoder can raise peaks. One factor for every part keeps their balance (and
            # their sum equal to the shifted recording); the returned gain makes up for it.
            peak = float(np.abs(y).max(initial=0))
            k = 0.999 / peak if peak >= 0.999 else 1.0
            os.makedirs(sd, exist_ok=True)
            for i, p in enumerate(PARTS):
                part = y[:, i * ch:(i + 1) * ch]
                write_flac(paths[p], part * np.float32(k) if k < 1 else part, sr)
            gain = m["gain"] / k
            store.write_json(info, {"gain": gain})
    return paths, gain


# ------------------------------------------------------------------ mixing & export
MAX_BOOST_DB = 20.0   # loudness matching never raises a (nearly silent) mix by more than this


def mix_parts(paths: dict[str, str], gains_db: dict[str, float | None], gain: float) -> tuple[np.ndarray, int]:
    """Sum parts with per-part gains (None = muted) -> ((frames, ch) float32, sr)."""
    total, sr = None, None
    for p in PARTS:
        g = gains_db.get(p, 0.0)
        if g is None:
            continue
        x, r = read_audio(paths[p])
        sr = sr or r
        x = x * np.float32(gain * 10 ** (g / 20))
        if total is None:
            total = x
        else:
            n = max(len(total), len(x))
            if len(total) < n:
                total = np.pad(total, ((0, n - len(total)), (0, 0)))
            total[:len(x)] += x
    if total is None:
        raise ValueError("Every part is muted, so there is nothing to export.")
    return total, sr


def render_mp3(y: np.ndarray, sr: int, dst: str, ref_rms: float | None = None) -> str:
    """Encode with the whole-recording shift's peak protection (never louder than ref_rms on average)."""
    if ref_rms is None:
        ref_rms = float(np.sqrt(np.mean(y.astype(np.float64) ** 2)))
    centers, gains = audio._clip_guard(y, ref_rms, sr=sr)
    store.write_with(dst, lambda tmp: audio._encode_mp3(y, centers, gains, tmp, sr=sr))
    return dst


class _Mp3Stream:
    """An ffmpeg MP3 encoder fed piece by piece, so a long session is never in memory at once."""

    def __init__(self, path: str, sr: int, channels: int):
        self.err = tempfile.TemporaryFile()
        self.proc = subprocess.Popen([ffmpeg_exe(), "-v", "error", "-y", "-f", "f32le", "-ar", str(sr), "-ac", str(channels),
                                      "-i", "pipe:0", "-codec:a", "libmp3lame", "-q:a", "2", "-f", "mp3", path],
                                     stdin=subprocess.PIPE, stderr=self.err)

    def write(self, y: np.ndarray) -> None:
        for s in range(0, len(y), 1 << 18):
            self.proc.stdin.write(np.ascontiguousarray(y[s:s + (1 << 18)], np.float32).tobytes())

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except BrokenPipeError:
            pass
        rc = self.proc.wait()
        self.err.seek(0)
        msg = self.err.read().decode(errors="replace").strip()[-400:]
        self.err.close()
        if rc:
            raise RuntimeError("ffmpeg could not encode the MP3: " + msg)


def export_session(items: list[dict], dst: str, gap_seconds: float = 4.0, lufs: float = -18.0,
                   progress: Callable[[int, str], None] = lambda i, t: None) -> dict:
    """Render a weekly session to one MP3: every item mixed in its key and tempo, peak-limited, and
    joined with silent gaps. Only one item is in memory at a time.

    Loudness is anchored on each bhajan's FULL recording: the gain that brings all of its parts to
    `lufs` is applied to the chosen mix. So every bhajan sits at the same level, and an
    accompaniment-only mix stays as much quieter as the music is under the singers, leaving room
    for the live voices (normalising the accompaniment itself would push it up to singing level).
    progress(i, title) may raise to cancel.

    items: {"dir", "title", "semitones", "tempo", "gains"} each. Returns track start times.
    """
    if not items:
        raise ValueError("The session is empty.")
    out_sr = MODEL_SR
    marks, t = [], 0.0

    def _w(tmp: str) -> None:
        nonlocal t
        enc = _Mp3Stream(tmp, out_sr, 2)
        try:
            for i, it in enumerate(items):
                progress(i, it.get("title", ""))
                paths, gain = shifted_parts(it["dir"], it.get("semitones", 0.0), it.get("tempo", 1.0))
                full, sr = mix_parts(paths, {}, gain)
                if full.shape[1] == 1:   # loudness is measured as the listener hears it: mono on both speakers
                    full = np.repeat(full, 2, axis=1)
                g_db = min(MAX_BOOST_DB, lufs - loudness(full, sr))
                del full
                y, sr = mix_parts(paths, it.get("gains", {}), gain)
                y = resample(y, sr, out_sr)
                if y.shape[1] == 1:
                    y = np.repeat(y, 2, axis=1)
                y *= np.float32(10 ** (g_db / 20))
                centers, curve = audio._clip_guard(y, float("inf"), sr=out_sr)   # peaks only, per item
                for s in range(0, len(y), 1 << 18):
                    y[s:s + (1 << 18)] *= np.interp(np.arange(s, s + len(y[s:s + (1 << 18)])), centers, curve).astype(np.float32)[:, None]
                if i:
                    enc.write(np.zeros((int(gap_seconds * out_sr), 2), np.float32))
                    t += gap_seconds
                marks.append({"title": it.get("title", ""), "start": round(t, 2)})
                enc.write(y)
                t += len(y) / out_sr
                del y
            progress(len(items), "Encoding")
        finally:
            enc.close()

    store.write_with(dst, _w)
    return {"tracks": marks, "seconds": round(t, 1)}


# ------------------------------------------------------------------ child entry point
def _main(argv: list[str]) -> int:
    import argparse
    import signal
    ap = argparse.ArgumentParser(prog="python -m bhajan.stems")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("split", help="separate SRC into parts in DIR")
    sp.add_argument("src")
    sp.add_argument("dir")
    sp.add_argument("--models", required=True)
    a = ap.parse_args(argv)
    os.environ.setdefault("TORCH_HOME", os.path.join(a.models, "torch"))

    def stop(signum, frame):     # Cancel (SIGTERM) or a closed Terminal (SIGHUP): unwind so `finally`s clean up
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGHUP, stop)

    def emit(ev: dict) -> None:
        sys.stdout.write(json.dumps(ev) + "\n")
        sys.stdout.flush()

    stage = {}

    def emit_stage(ev: dict) -> None:
        if "stage" in ev:
            stage.clear()
            stage.update({k: ev[k] for k in ("stage", "step", "steps", "msg")})
        emit(stage | ev)

    try:
        m = split(a.src, a.dir, a.models, emit_stage)
    except Exception as e:
        import traceback
        traceback.print_exc()
        emit({"error": f"{type(e).__name__}: {e}"})
        return 1
    emit({"manifest": m})
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
