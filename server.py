"""Bhajan Studio: transpose Sai Rhythms bhajans to your key.

Run:  ./start.sh  or  start.bat       (installs what it needs, opens your browser)
  or  uv run server.py [--open] [--port N] [--host H]

The PORT environment variable sets the default port (8777). If the port is busy, the next free
one (up to +20) is used and printed. The server only listens on this computer (127.0.0.1) unless
--host (or BHAJAN_HOST) says otherwise.

On a server (see docs/DEPLOY.md):
  BHAJAN_PASSWORD       every request needs this password, except /api/health and the files a
                        browser needs to install the app: browsers get a sign-in page (and a
                        30-day cookie), scripts can send it as HTTP Basic auth (any user name)
  BHAJAN_ALLOWED_HOSTS  comma-separated host names the server answers to, e.g. bhajans.example.org
                        (127.0.0.1 and localhost always work); unset = any name, when listening publicly
  BHAJAN_DATA           the cache folder (default: data/ next to this file)
"""
from __future__ import annotations

if __name__ == "__main__":  # say something before the heavy imports below
    print("Starting Bhajan Studio...", flush=True)

import argparse
import asyncio
import base64
import errno
import functools
import hashlib
import hmac
import json
import mimetypes
import os
import re
import shutil
import socket
import sys
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import webbrowser
from typing import Literal

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bhajan import audio, sheet, stems, store
from bhajan.sairhythms import SaiRhythms, canonical_slug

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("BHAJAN_DATA", os.path.join(ROOT, "data"))
MEDIA = os.path.join(DATA, "media")
MEDIA_CAP = 500 * 1024 * 1024   # rendered/shifted audio kept in data/media; least recently used goes first
STEMS = os.path.join(DATA, "stems")     # split parts, one folder per recording (with its shifted copies)
STEMS_CAP = 3 * 1024 ** 3               # least recently used recordings' parts go first
MODELS = os.path.join(DATA, "models")   # separation model weights, downloaded on first split
SHIFT_VERSION = "pylibrb-r3-limiter-1"   # change when pitch_shift's output changes, to re-shift
KEY_VERSION = 3                          # same for analyze_key's result
os.makedirs(MEDIA, exist_ok=True)
os.makedirs(STEMS, exist_ok=True)
PASSWORD = os.environ.get("BHAJAN_PASSWORD", "")
try:
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as f:
        VERSION = tomllib.load(f)["project"]["version"]
except (OSError, KeyError, tomllib.TOMLDecodeError):
    VERSION = "unknown"

# Python takes file types from the Windows registry, where .js or .css can be text/plain (browsers
# then refuse the stylesheet); say them here so every platform serves the same types.
for _ext, _type in {".js": "text/javascript", ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml",
                    ".png": "image/png", ".webmanifest": "application/manifest+json", ".json": "application/json",
                    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".flac": "audio/flac", ".mid": "audio/midi"}.items():
    mimetypes.add_type(_type, _ext)

Instrument = Literal["flute", "piano", "harmonium"]

sr = SaiRhythms(DATA)
splits = stems.SplitJobs(MODELS)
app = FastAPI(title="Bhajan Studio")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _h(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def _song(slug: str, refresh: bool = False) -> dict:
    try:
        return sr.song(slug, refresh=refresh)
    except Exception as e:
        raise HTTPException(502, f"Could not load this song from Sai Rhythms: {e}")


def _is_404(e: Exception) -> bool:
    return isinstance(e, urllib.error.HTTPError) and e.code == 404


def _score(slug: str, sheet_i: int) -> dict:
    info = _song(slug)
    if not info["sheets"]:
        raise HTTPException(404, "This bhajan has no sheet music on Sai Rhythms.")
    if not (0 <= sheet_i < len(info["sheets"])):
        raise HTTPException(404, "No such sheet")
    path = info["sheets"][sheet_i]
    try:
        try:
            pdf = sr.sheet_pdf(path)
        except Exception as e:
            if not _is_404(e):
                raise
            info = _song(slug, refresh=True)   # the site may have moved it: re-read the song page once
            if sheet_i >= len(info["sheets"]) or info["sheets"][sheet_i] == path:
                raise
            path = info["sheets"][sheet_i]
            pdf = sr.sheet_pdf(path)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not download the sheet music from Sai Rhythms: {e}")
    work = os.path.join(DATA, "scores", _h(info["slug"], path))
    with _lock("score" + work):
        sc = sheet.read_sheet(pdf, work)
    # identifies the notes read, so renders and MIDI files are redone when the reader improves
    return sc | {"work": work, "fp": _h(sc["schema"], sc["events"], sc.get("bar"))}


def _recording(slug: str, i: int) -> tuple[str, str]:
    """(local mp3 path, its Sai Rhythms URL) of recording i."""
    info = _song(slug)
    if not (0 <= i < len(info["audio"])):
        raise HTTPException(404, "This bhajan has no such recording on Sai Rhythms.")
    url = info["audio"][i]["url"]
    try:
        try:
            return sr.audio_file(url), url
        except Exception as e:
            if not _is_404(e):
                raise
            info = _song(slug, refresh=True)   # the site may have renamed it: re-read the song page once
            if i >= len(info["audio"]) or info["audio"][i]["url"] == url:
                raise
            url = info["audio"][i]["url"]
            return sr.audio_file(url), url
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"Could not download the recording: {e}")


def _melody_hist(score: dict) -> list[float]:
    h = np.zeros(12)
    for e in score["events"]:
        if e["kind"] == "note":
            h[(e["midi"] - 60) % 12] += e["dur"]
    return list(h)


def _touch(*paths: str) -> None:
    """Mark cached media as just used (the pruning below removes the least recently used first)."""
    for p in paths:
        try:
            os.utime(p)
        except OSError:
            pass


_prune_lock = threading.Lock()


def _prune_media(cap: int = MEDIA_CAP) -> None:
    """Keep data/media under `cap` bytes by deleting the least recently used renders, MIDI files and
    shifted recordings (they are made again on demand). Files being made or just used are kept."""
    if not _prune_lock.acquire(blocking=False):
        return
    try:
        now = time.time()
        files = []
        for name in os.listdir(MEDIA):
            p = os.path.join(MEDIA, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if ".part" in name:
                if now - st.st_mtime > 3600:  # left behind by a crash
                    os.remove(p)
                continue
            key, ext = os.path.splitext(name)
            if ext in (".wav", ".mp3", ".mid"):
                files.append((st.st_mtime, st.st_size, key, ext, p))
        total = sum(f[1] for f in files)
        for mtime, size, key, ext, p in sorted(files):
            if total <= cap:
                break
            lk = _locks.get(key)
            if now - mtime < 60 or (lk is not None and lk.locked()):
                continue
            try:
                os.remove(p)
                if ext == ".wav" and os.path.exists(os.path.join(MEDIA, key + ".json")):
                    os.remove(os.path.join(MEDIA, key + ".json"))
            except OSError:
                continue
            total -= size
    except OSError:
        pass
    finally:
        _prune_lock.release()


# ------------------------------------------------------------------ routes
@functools.cache
def _ocr_engine() -> str:
    from bhajan import omr
    try:
        return omr.ocr_engine()
    except ValueError:      # a misspelt BHAJAN_OCR: reading a sheet says so
        return "none"


@app.get("/api/health")
def health():
    """For uptime checks and Docker's HEALTHCHECK: needs no password, and says nothing about bhajans."""
    return {"ok": True, "version": VERSION, "parts": stems.parts_available(), "ocr": _ocr_engine()}


@app.get("/api/search")
def search(q: str = "", limit: int = Query(40, ge=1, le=200)):
    return sr.search(q, limit)


@app.get("/api/song/{slug}")
def song(slug: str):
    return _song(slug)


@app.post("/api/song/{slug}/score")
def score(slug: str, sheet_i: int = Query(0, ge=0)):
    sc = _score(slug, sheet_i)
    out = {k: v for k, v in sc.items() if k not in ("work", "fp")}
    s = urllib.parse.quote(canonical_slug(slug), safe="")
    out["page_urls"] = [f"/api/song/{s}/page/{sheet_i}/{p}.png" for p in range(len(sc["pages"]))]
    return out


@app.get("/api/song/{slug}/page/{sheet_i}/{page}.png")
def page_png(slug: str, sheet_i: int, page: int):
    sc = _score(slug, sheet_i)
    p = os.path.join(sc["work"], f"page{page}.png")
    if not os.path.exists(p):
        raise HTTPException(404)
    return FileResponse(p, media_type="image/png")


class RenderReq(BaseModel):
    sheet_i: int = Field(0, ge=0)
    sa_midi: int = Field(60, ge=36, le=84)
    tempo: float = Field(90, ge=30, le=300)
    instrument: Instrument = "flute"
    drone: bool = False
    drone_kind: Literal["pa", "ma"] = "pa"
    click: bool = False


def _sound_tag(synth, instrument: str) -> str:
    """synth.cache_tag, after trying to fetch the samples: a render made with the offline fallback is
    cached under the fallback's tag, so the real samples are used once they can be downloaded."""
    if instrument != "flute":
        from bhajan import instruments
        try:
            instruments.ensure(instrument)
        except Exception:
            pass  # synth.render falls back to the synthesized sound and says so
    return synth.cache_tag(instrument)


@app.post("/api/song/{slug}/render")
def render(slug: str, req: RenderReq):
    from bhajan import synth
    sc = _score(slug, req.sheet_i)
    key = _h("render", sc["fp"], req.model_dump(), _sound_tag(synth, req.instrument))
    wav = os.path.join(MEDIA, key + ".wav")
    meta = os.path.join(MEDIA, key + ".json")
    with _lock(key):
        m = store.read_json(meta) if os.path.exists(wav) else None
        if m is None:
            try:
                y, timings = synth.render(sc["events"], req.sa_midi, req.tempo, instrument=req.instrument,
                                          drone=req.drone, drone_kind=req.drone_kind, click=req.click,
                                          beats_per_bar=sc.get("bar") or 4)
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(500, f"Could not render the {req.instrument} audio: {e}")
            store.write_with(wav, lambda tmp: synth.write_wav(tmp, y, 32000))
            m = {"timings": timings, "duration": len(y) / 32000}
            store.write_json(meta, m)
        else:
            _touch(wav, meta)
    _prune_media()
    return {"audio_url": f"/media/{key}.wav", **m}


@app.get("/api/song/{slug}/midi")
def midi(slug: str, sheet_i: int = Query(0, ge=0), sa_midi: int = Query(60, ge=36, le=84),
         tempo: float = Query(90, ge=30, le=300), instrument: Instrument = "flute"):
    from bhajan import synth
    sc = _score(slug, sheet_i)
    key = _h("midi", sc["fp"], sa_midi, tempo, instrument, os.path.getmtime(synth.__file__))
    path = os.path.join(MEDIA, key + ".mid")
    with _lock(key):
        if not os.path.exists(path):
            store.write_with(path, lambda tmp: synth.write_midi(tmp, sc["events"], sa_midi, tempo,
                                                                instrument=instrument))
        else:
            _touch(path)
    _prune_media()
    return FileResponse(path, media_type="audio/midi", filename=f"{slug}-sa{sa_midi}.mid")


@app.get("/api/song/{slug}/audio/{i}")
def original_audio(slug: str, i: int):
    path, _ = _recording(slug, i)
    return FileResponse(path, media_type="audio/mpeg")


@app.post("/api/song/{slug}/analyze")
def analyze(slug: str, audio_i: int = Query(0, ge=0)):
    info = _song(slug)
    if not (0 <= audio_i < len(info["audio"])):
        raise HTTPException(404, "This bhajan has no recording on Sai Rhythms.")
    hist = None
    if info["sheets"]:
        try:
            hist = _melody_hist(_score(slug, 0))
        except Exception:
            hist = None
    path, url = _recording(slug, audio_i)
    key = _h("key", info["slug"], url, KEY_VERSION, hist and [round(x, 4) for x in hist])
    cache = os.path.join(MEDIA, key + ".json")
    with _lock(key):
        res = store.read_json(cache)
        if res is not None:
            return res
        priors = [p["pc"] for p in (info.get("gents_pitch"), info.get("ladies_pitch")) if p and p.get("pc") is not None]
        try:
            res = audio.analyze_key(path, info["raga_aroha"] + info["raga_avaroha"], melody_hist=hist, priors=priors)
        except Exception as e:
            raise HTTPException(500, f"Could not analyse the recording: {e}")
        if not res.get("ok"):
            raise HTTPException(422, res.get("error", "Could not analyse the recording"))
        store.write_json(cache, res)
    return res


class ShiftReq(BaseModel):
    audio_i: int = Field(0, ge=0)
    semitones: float = Field(ge=-12.5, le=12.5)
    formant: bool = True


@app.post("/api/song/{slug}/shift")
def shift(slug: str, req: ShiftReq):
    src, url = _recording(slug, req.audio_i)
    key = _h("shift", canonical_slug(slug), url, round(req.semitones, 3), req.formant, SHIFT_VERSION)
    dst = os.path.join(MEDIA, key + ".mp3")
    with _lock(key):
        if not os.path.exists(dst):
            try:
                audio.pitch_shift(src, dst, req.semitones, formant=req.formant)
            except Exception as e:
                raise HTTPException(500, f"Could not pitch-shift the recording: {e}")
        else:
            _touch(dst)
    _prune_media()
    return {"audio_url": f"/media/{key}.mp3"}


# ------------------------------------------------------------------ parts (singing / tabla / harmonium)
def _parts_dir(slug: str, audio_i: int) -> tuple[str, str, str]:
    """(folder of the split parts, local recording path, job key) for recording audio_i."""
    src, url = _recording(slug, audio_i)
    key = _h("stems", canonical_slug(slug), url, stems.STEMS_VERSION)
    return os.path.join(STEMS, key), src, key


def _song_tags(slug: str) -> set[str]:
    slug = canonical_slug(slug)
    try:
        return next((set(t.lower() for t in x.get("tags", [])) for x in sr.index() if x["slug"] == slug), set())
    except Exception:
        return set()


# Recordings of Swami's own voice are not taken apart. Songs Sai Rhythms tags "karaoke" or
# "instrumental" already have an official accompaniment recording, which beats any split.
NO_SPLIT_TAGS = {"golden voice"}
OFFICIAL_TAGS = {"karaoke", "instrumental"}


def _parts_status(d: str, key: str, slug: str = "") -> dict:
    tags = _song_tags(slug) if slug else set()
    if tags & NO_SPLIT_TAGS:
        return {"state": "unavailable",
                "msg": "This is a recording of Swami's own voice, so Bhajan Studio does not split it into parts."}
    s = splits.status(key, d)
    if tags & OFFICIAL_TAGS:
        s["official"] = sorted(tags & OFFICIAL_TAGS)
    m = stems.read_manifest(d) if s["state"] == "done" else None
    if m:
        s |= {"parts": [{"id": p, "label": stems.LABELS[p], "share_db": m["parts"][p]["share_db"]} for p in stems.PARTS],
              "models": m["models"], "seconds": m["seconds"], "sr": m["sr"]}
    return s


@app.get("/api/song/{slug}/parts")
def parts_status(slug: str, audio_i: int = Query(0, ge=0)):
    if _song_tags(slug) & NO_SPLIT_TAGS:          # answer before downloading anything
        return _parts_status("", "", slug)
    d, _, key = _parts_dir(slug, audio_i)
    return _parts_status(d, key, slug)


@app.post("/api/song/{slug}/parts")
def parts_split(slug: str, audio_i: int = Query(0, ge=0)):
    if _song_tags(slug) & NO_SPLIT_TAGS:
        raise HTTPException(403, "This is a recording of Swami's own voice, so Bhajan Studio does not split it into parts.")
    d, src, key = _parts_dir(slug, audio_i)
    splits.start(key, src, d)
    _prune_stems()
    return _parts_status(d, key, slug)


@app.delete("/api/song/{slug}/parts")
def parts_cancel(slug: str, audio_i: int = Query(0, ge=0)):
    d, _, key = _parts_dir(slug, audio_i)
    splits.cancel(key)
    return _parts_status(d, key, slug)


class PartsReq(BaseModel):
    audio_i: int = Field(0, ge=0)
    semitones: float = Field(0, ge=-12.5, le=12.5)
    tempo: float = Field(1.0, ge=0.7, le=1.3)


def _shifted(d: str, semitones: float, tempo: float) -> tuple[dict[str, str], float]:
    try:
        return stems.shifted_parts(d, round(semitones, 3), round(tempo, 3))
    finally:
        _prune_stems()


@app.post("/api/song/{slug}/parts/render")
def parts_render(slug: str, req: PartsReq):
    """Every part in the requested key and tempo, for the browser to mix live."""
    d, _, _ = _parts_dir(slug, req.audio_i)
    m = stems.read_manifest(d)
    if not m:
        raise HTTPException(409, "Split the recording into parts first.")
    try:
        paths, gain = _shifted(d, req.semitones, req.tempo)
    except Exception as e:
        raise HTTPException(500, f"Could not shift the parts: {e}")
    return {"gain": gain, "sr": m["sr"],
            "urls": {p: "/stems/" + os.path.relpath(paths[p], STEMS).replace(os.sep, "/") for p in stems.PARTS}}


class MixReq(PartsReq):
    gains: dict[str, float | None] = Field(default_factory=dict)   # dB per part; null = muted


def _clean_gains(g: dict[str, float | None]) -> dict[str, float | None]:
    bad = set(g) - set(stems.PARTS)
    if bad:
        raise HTTPException(422, f"Unknown part(s): {', '.join(sorted(bad))}")
    gains = {p: (None if v is None else max(-60.0, min(12.0, float(v)))) for p, v in g.items()}
    if all(gains.get(p, 0.0) is None for p in stems.PARTS):
        raise HTTPException(422, "Every part is muted, so there is nothing to export.")
    return gains


@app.post("/api/song/{slug}/parts/mix")
def parts_mix(slug: str, req: MixReq):
    """Render the chosen mix of parts to an MP3 (for download)."""
    d, _, _ = _parts_dir(slug, req.audio_i)
    m = stems.read_manifest(d)
    if not m:
        raise HTTPException(409, "Split the recording into parts first.")
    gains = _clean_gains(req.gains)
    key = _h("partsmix", d, round(req.semitones, 3), round(req.tempo, 3), gains, stems.SHIFT_VERSION)
    dst = os.path.join(MEDIA, key + ".mp3")
    with _lock(key):
        if not os.path.exists(dst):
            try:
                paths, gain = _shifted(d, req.semitones, req.tempo)
                y, sr_ = stems.mix_parts(paths, gains, gain)
                stems.render_mp3(y, sr_, dst, ref_rms=m["rms"])   # a mix is never louder than the original
            except Exception as e:
                raise HTTPException(500, f"Could not mix the parts: {e}")
        else:
            _touch(dst)
    _prune_media()
    return {"audio_url": f"/media/{key}.mp3"}


class SessionItem(BaseModel):
    slug: str
    audio_i: int = Field(0, ge=0)
    title: str = Field("", max_length=200)
    semitones: float = Field(0, ge=-12.5, le=12.5)
    tempo: float = Field(1.0, ge=0.7, le=1.3)
    gains: dict[str, float | None] = Field(default_factory=dict)


class SessionReq(BaseModel):
    name: str = Field("Bhajan session", max_length=120)
    gap: float = Field(4.0, ge=0, le=30)
    items: list[SessionItem] = Field(min_length=1, max_length=40)


_sessions: dict[str, dict] = {}     # export jobs by key; the oldest finished ones are dropped
_sessions_guard = threading.Lock()
_export_lock = threading.Lock()     # one export at a time: each holds a bhajan's audio in memory
_exporting: dict[str, int] = {}     # recording folders that queued/running exports still need: never pruned


class _Cancelled(Exception):
    pass


def _session_view(key: str, job: dict) -> dict:
    j = dict(job)                   # a C-level copy: the export thread may be updating the job
    j.pop("path", None)
    j.pop("cancel", None)
    return j | {"id": key}


@app.post("/api/session/export")
def session_export(req: SessionReq):
    """Start rendering a whole session (every bhajan mixed, in its key, loudness-matched) to one MP3."""
    items = []
    for it in req.items:
        d, _, _ = _parts_dir(it.slug, it.audio_i)
        if not stems.read_manifest(d):
            raise HTTPException(409, f"'{it.title or it.slug}' has not been split into parts yet.")
        items.append({"dir": d, "title": it.title or it.slug, "semitones": round(it.semitones, 3),
                      "tempo": round(it.tempo, 3), "gains": _clean_gains(it.gains)})
    key = _h("session", [(i["dir"], i["semitones"], i["tempo"], i["gains"], i["title"]) for i in items],
             req.gap, stems.SHIFT_VERSION)
    dst = os.path.join(MEDIA, key + ".mp3")
    with _sessions_guard:
        job = _sessions.get(key)
        if job and (job["state"] in ("queued", "running") or (job["state"] == "done" and os.path.exists(dst))):
            job["cancel"] = False       # someone wants it again (an earlier page may have given up on it)
            _touch(dst)
            return _session_view(key, job)
        job = _sessions[key] = {"state": "queued", "done": 0, "total": len(items), "msg": "Waiting for another export",
                                "path": dst, "cancel": False}
        done = [k for k, j in _sessions.items() if j["state"] not in ("queued", "running")]
        for k in done[:-20]:
            del _sessions[k]
        names = {os.path.basename(i["dir"]) for i in items}
        for n in names:
            _exporting[n] = _exporting.get(n, 0) + 1
            stems.touch(os.path.join(STEMS, n))

    def run():
        try:
            with _export_lock:
                if job["cancel"]:
                    job.update(state="cancelled")
                    return
                job.update(state="running", msg="Starting")

                def progress(i, title):
                    if job["cancel"]:
                        raise _Cancelled()
                    job.update(done=i, msg=title)
                try:
                    info = stems.export_session(items, dst, gap_seconds=req.gap, progress=progress)
                    job.update(state="done", done=len(items), msg="Done", audio_url=f"/media/{key}.mp3", **info)
                except _Cancelled:
                    job.update(state="cancelled")
                except Exception as e:
                    job.update(state="error", error=str(e))
        finally:
            with _sessions_guard:
                for n in names:
                    _exporting[n] -= 1
                    if not _exporting[n]:
                        del _exporting[n]
        _prune_media()
        _prune_stems()
    threading.Thread(target=run, daemon=True, name="session-export").start()
    return _session_view(key, job)


@app.delete("/api/session/export/{job_id}")
def session_cancel(job_id: str):
    """Stop an export the page no longer wants (the session changed): it ends before its next bhajan."""
    job = _sessions.get(job_id)
    if not job:
        raise HTTPException(404, "No such export.")
    if job["state"] in ("queued", "running"):
        job["cancel"] = True
    return _session_view(job_id, job)


@app.get("/api/session/export/{job_id}")
def session_status(job_id: str):
    job = _sessions.get(job_id)
    if not job:
        raise HTTPException(404, "No such export.")
    if job["state"] == "done":
        if not os.path.exists(job["path"]):     # pruned from data/media since: export again
            return {"state": "expired", "id": job_id, "error": "This export was cleared from the cache; export it again."}
        _touch(job["path"])
    return _session_view(job_id, job)


_prune_stems_lock = threading.Lock()


def _prune_stems(cap: int = STEMS_CAP) -> None:
    """Keep data/stems under `cap` bytes. Shifted copies (cheap to redo) go first, least recently
    used key first; only then whole recordings' splits (a GPU split to redo), least recently used
    first. Use is recorded in mtimes (APFS does not update atimes); a split in progress, or anything
    used in the last 10 minutes, is kept."""
    if not _prune_stems_lock.acquire(blocking=False):
        return
    try:
        def size(p: str) -> int:
            n = 0
            for root, _, files in os.walk(p):
                for f in files:
                    try:
                        n += os.path.getsize(os.path.join(root, f))
                    except OSError:
                        pass
            return n

        now = time.time()
        with _sessions_guard:
            busy = splits.busy() | set(_exporting)
        shifts, songs, total = [], [], 0
        for name in os.listdir(STEMS):
            d = os.path.join(STEMS, name)
            if not os.path.isdir(d):
                continue
            sd = os.path.join(d, "shift")
            for k in (os.listdir(sd) if os.path.isdir(sd) else []):
                p = os.path.join(sd, k)
                sz = size(p)
                shifts.append((os.path.getmtime(p), sz, p, name))
                total += sz
            whole = size(d)
            own = whole - sum(s[1] for s in shifts if s[3] == name)
            total += own
            try:
                used = os.path.getmtime(os.path.join(d, "manifest.json"))
            except OSError:
                used = os.path.getmtime(d)
            songs.append((used, own, d, name))
        for used, sz, p, name in sorted(shifts):
            if total <= cap:
                return
            if name in busy or now - used < 600:
                continue
            lk = store.lock(p)               # the lock shifted_parts holds while it renders or reuses this key
            if not lk.acquire(blocking=False):
                continue
            try:
                if now - os.path.getmtime(p) < 600:      # used since we looked
                    continue
                shutil.rmtree(p, ignore_errors=True)
            except OSError:
                continue
            finally:
                lk.release()
            total -= sz
        for used, sz, d, name in sorted(songs):
            if total <= cap:
                return
            with _sessions_guard:
                busy = splits.busy() | set(_exporting)
            try:
                used = os.path.getmtime(os.path.join(d, "manifest.json"))
            except OSError:
                pass
            if name in busy or now - used < 600:
                continue
            shutil.rmtree(d, ignore_errors=True)
            splits.forget(name)
            total -= sz
    except OSError:
        pass
    finally:
        _prune_stems_lock.release()


# Only this computer's own browser may use the app: the Host check stops DNS-rebinding pages, and
# the Origin / Sec-Fetch-Site checks stop other websites from driving the API through the browser
# (which matters on a server too: browsers send a saved password along with other sites' requests).
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
_allow_any_host = False   # set by main() when the user explicitly listens on another address


def _host_name(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):
        return host[: host.find("]") + 1]
    return host.split(":", 1)[0]


# A reverse proxy on the same computer (Caddy, nginx) passes on the public name, so names listed here
# are accepted on any address; when listening publicly, they are the only ones (if any are listed).
ALLOWED_HOSTS = {_host_name(h) for h in os.environ.get("BHAJAN_ALLOWED_HOSTS", "").split(",") if h.strip()}


def _host_ok(host: str) -> bool:
    name = _host_name(host)
    return name in _LOCAL_HOSTS or name in ALLOWED_HOSTS or (_allow_any_host and not ALLOWED_HOSTS)


# With BHAJAN_PASSWORD, a browser without the sign-in cookie gets static/login.html instead of the
# app (a 200, so hosts that check the home page, such as Replit, see the app is up), and every other
# request a 401. Scripts and uptime monitors can send the password as HTTP Basic auth instead.
# What a browser fetches without the password: the health check, the sign-in page and what it needs
# to offer to install the app (it asks for the manifest and icons without credentials). None of it
# is private.
_PUBLIC_PATHS = {"/api/health", "/login.html", "/manifest.webmanifest", "/sw.js"}
_PUBLIC_RE = re.compile(r"/icons/[A-Za-z0-9_-]+\.(?:png|svg|ico)")
SESSION_COOKIE = "bhajan_session"
SESSION_DAYS = 30


def _session_token(issued: int) -> str:
    """'<time issued>.<signature>': only this password can sign one, and a new password ends them all."""
    key = hashlib.sha256(b"bhajan-studio session\0" + PASSWORD.encode()).digest()
    return f"{issued}.{hmac.new(key, str(issued).encode(), 'sha256').hexdigest()}"


def _signed_in(request) -> bool:
    token = request.cookies.get(SESSION_COOKIE, "")
    issued = token.partition(".")[0]
    if issued.isascii() and issued.isdigit() and time.time() - int(issued) < SESSION_DAYS * 86400:
        return hmac.compare_digest(token, _session_token(int(issued)))
    scheme, _, cred = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        _, sep, given = base64.b64decode(cred.strip(), validate=True).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return bool(sep) and hmac.compare_digest(given.encode(), PASSWORD.encode())


@app.post("/api/login")
async def login(request: Request):
    """The sign-in page's form. A wrong password costs a second, to slow down guessing."""
    body = b""
    async for chunk in request.stream():
        body += chunk
        if len(body) > 4096:
            break
    given = urllib.parse.parse_qs(body[:4096].decode("utf-8", "replace")).get("password", [""])[0]
    if PASSWORD and not hmac.compare_digest(given.encode(), PASSWORD.encode()):
        await asyncio.sleep(1)
        return RedirectResponse("/?wrong", status_code=303)
    resp = RedirectResponse("/", status_code=303)
    if PASSWORD:
        https = request.url.scheme == "https" or \
            request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower() == "https"
        resp.set_cookie(SESSION_COOKIE, _session_token(int(time.time())), max_age=SESSION_DAYS * 86400,
                        path="/", httponly=True, samesite="lax", secure=https)
    return resp


@app.middleware("http")
async def _guard_and_revalidate(request, call_next):
    host = request.headers.get("host", "")
    path = request.scope["path"]
    public = (request.method in ("GET", "HEAD") and (path in _PUBLIC_PATHS or _PUBLIC_RE.fullmatch(path))) or \
        (request.method == "POST" and path == "/api/login")
    if not _host_ok(host) and path != "/api/health":     # load balancers check health by IP address
        msg = ("This server does not answer to that host name (see BHAJAN_ALLOWED_HOSTS)." if _allow_any_host or ALLOWED_HOSTS
               else "Bhajan Studio only answers requests addressed to 127.0.0.1 or localhost.")
        return JSONResponse({"detail": msg}, status_code=403)
    if PASSWORD and not public and not _signed_in(request):
        if request.method in ("GET", "HEAD") and path in ("/", "/index.html"):
            return FileResponse(os.path.join(ROOT, "static", "login.html"), media_type="text/html",
                                headers={"Cache-Control": "no-store"})
        return JSONResponse({"detail": "Sign in to Bhajan Studio first (reload the page)."}, status_code=401)
    if path.startswith(("/api/", "/stems/")):   # /stems: other sites may not probe which bhajans were split
        site = request.headers.get("sec-fetch-site")
        origin = request.headers.get("origin")
        if (site and site not in ("same-origin", "none")) or \
                (origin is not None and urllib.parse.urlsplit(origin).netloc.lower() != host.lower()):
            return JSONResponse({"detail": "Requests from other websites are not allowed."}, status_code=403)
    resp = await call_next(request)
    if not request.url.path.startswith(("/media/", "/stems/")):   # both are content-addressed
        resp.headers["Cache-Control"] = "no-cache"
    return resp


app.mount("/media", StaticFiles(directory=MEDIA), name="media")
app.mount("/stems", StaticFiles(directory=STEMS), name="stems")
app.mount("/", StaticFiles(directory=os.path.join(ROOT, "static"), html=True), name="static")


@app.exception_handler(Exception)
async def _err(request, exc):  # pragma: no cover - surface errors as JSON for the UI
    return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)


# ------------------------------------------------------------------ launcher
PORT_TRIES = 21  # the requested port plus the next 20


def _port_in_use(host: str, port: int) -> bool:
    """True if something already accepts connections on host:port."""
    probe = {"": "127.0.0.1", "0.0.0.0": "127.0.0.1", "::": "::1"}.get(host, host)
    try:
        with socket.create_connection((probe, port), timeout=0.3):
            return True
    except OSError:
        return False


def _bind(host: str, port: int) -> socket.socket | None:
    """Bind a listening socket on host:port, or return None if the port is taken."""
    if _port_in_use(host, port):
        return None
    infos = socket.getaddrinfo(host or None, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE)
    err: OSError | None = None
    for family, socktype, proto, _, addr in infos:
        sock = socket.socket(family, socktype, proto)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind(addr)
            return sock
        except OSError as e:
            sock.close()
            err = e
    if err is not None and err.errno != errno.EADDRINUSE:
        raise err
    return None


def _browser_host(host: str) -> str:
    if host in ("", "0.0.0.0"):
        return "127.0.0.1"
    if host == "::":
        return "[::1]"
    return f"[{host}]" if ":" in host else host


def _when_ready(server: "uvicorn.Server", url: str, notes: list[str], open_browser: bool) -> None:
    """Print the banner (and open the browser) once the server accepts connections."""
    deadline = time.monotonic() + 120
    while not server.started:
        if server.should_exit or time.monotonic() > deadline:
            return
        time.sleep(0.05)
    lines = [
        "",
        "  Bhajan Studio is running.",
        "",
        f"    Open in your browser:  {url}",
    ]
    lines += [f"                           {n}" for n in notes]
    lines += [
        f"    Cache folder:          {DATA}",
        "    To stop it:            press Ctrl+C here, or close this window.",
        "",
    ]
    print("\n".join(lines), flush=True)
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception as e:  # pragma: no cover - depends on the desktop session
            print(f"  (Could not open the browser automatically: {e}. Open the address above.)", flush=True)


def main(argv: list[str] | None = None) -> None:
    global _allow_any_host

    def port_number(text: str) -> int:
        try:
            n = int(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"not a port number: {text!r}")
        if not 1 <= n <= 65535:
            raise argparse.ArgumentTypeError(f"port must be between 1 and 65535, not {n}")
        return n

    ap = argparse.ArgumentParser(description=f"Run the Bhajan Studio web app (version {VERSION}).")
    ap.add_argument("--host", default=os.environ.get("BHAJAN_HOST") or "127.0.0.1",
                    help="address to listen on (default: $BHAJAN_HOST or 127.0.0.1, this computer only). "
                         "Anything else, e.g. 0.0.0.0, lets other devices use the app: set BHAJAN_PASSWORD then.")
    ap.add_argument("--port", type=port_number, default=os.environ.get("PORT") or "8777",
                    help="port to listen on; if it is busy the next free one is used (default: $PORT or 8777)")
    ap.add_argument("--open", action="store_true", help="open the app in the default browser once it is ready")
    args = ap.parse_args(argv)

    sock = None
    last = min(args.port + PORT_TRIES - 1, 65535)
    try:
        for port in range(args.port, last + 1):
            sock = _bind(args.host, port)
            if sock is not None:
                break
    except OSError as e:
        sys.exit(f"Bhajan Studio could not listen on {args.host}:{port}: {e}")
    if sock is None:
        sys.exit(f"Bhajan Studio could not start: ports {args.port}-{last} are all in use.\n"
                 "Pick another one, e.g.  " + ("start.bat -Port 9000" if os.name == "nt" else "./start.sh --port 9000"))

    port = sock.getsockname()[1]
    url = f"http://{_browser_host(args.host)}:{port}"
    notes = []
    if port != args.port:
        notes += [f"Port {args.port} was busy, so this uses {port}.",
                  "(Is Bhajan Studio already open in another window?)"]
    if args.host.strip().lower() not in _LOCAL_HOSTS | {"::1"}:
        _allow_any_host = True
        where = args.host or "all interfaces"
        if PASSWORD:
            notes.append(f"Listening on {where}; other devices need the password (BHAJAN_PASSWORD).")
        else:
            notes.append(f"WARNING: listening on {where} with no password, so anyone who can reach this "
                         "computer can use the app. Set BHAJAN_PASSWORD to require one.")
        if ALLOWED_HOSTS:
            notes.append("Answers to: " + ", ".join(sorted(ALLOWED_HOSTS | _LOCAL_HOSTS)))
    elif PASSWORD:
        notes.append("Password required (BHAJAN_PASSWORD).")
    _prune_media()

    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=port, log_level="warning"))
    threading.Thread(target=_when_ready, args=(server, url, notes, args.open), daemon=True).start()
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
