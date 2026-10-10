"""Bhajan Studio: transpose Sai Rhythms bhajans to your key.

Run:  uv run server.py      then open http://127.0.0.1:8777
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from typing import Optional

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from bhajan import audio, sheet
from bhajan.sairhythms import SaiRhythms

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("BHAJAN_DATA", os.path.join(ROOT, "data"))
MEDIA = os.path.join(DATA, "media")
os.makedirs(MEDIA, exist_ok=True)

sr = SaiRhythms(DATA)
app = FastAPI(title="Bhajan Studio")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock(key: str) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def _h(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()[:16]


def _song(slug: str) -> dict:
    try:
        return sr.song(slug)
    except Exception as e:
        raise HTTPException(502, f"Could not load this song from Sai Rhythms: {e}")


def _score(slug: str, sheet_i: int) -> dict:
    info = _song(slug)
    if not info["sheets"]:
        raise HTTPException(404, "This bhajan has no sheet music on Sai Rhythms.")
    if not (0 <= sheet_i < len(info["sheets"])):
        raise HTTPException(404, "No such sheet")
    work = os.path.join(DATA, "scores", _h(slug, info["sheets"][sheet_i]))
    with _lock("score" + work):
        pdf = sr.sheet_pdf(info["sheets"][sheet_i])
        return sheet.read_sheet(pdf, work) | {"work": work}


def _melody_hist(score: dict) -> list[float]:
    h = np.zeros(12)
    for e in score["events"]:
        if e["kind"] == "note":
            h[(e["midi"] - 60) % 12] += e["dur"]
    return list(h)


# ------------------------------------------------------------------ routes
@app.get("/api/search")
def search(q: str = "", limit: int = 40):
    return sr.search(q, limit)


@app.get("/api/song/{slug}")
def song(slug: str):
    return _song(slug)


@app.post("/api/song/{slug}/score")
def score(slug: str, sheet_i: int = 0):
    sc = _score(slug, sheet_i)
    out = {k: v for k, v in sc.items() if k != "work"}
    out["page_urls"] = [f"/api/song/{slug}/page/{sheet_i}/{p}.png" for p in range(len(sc["pages"]))]
    return out


@app.get("/api/song/{slug}/page/{sheet_i}/{page}.png")
def page_png(slug: str, sheet_i: int, page: int):
    sc = _score(slug, sheet_i)
    p = os.path.join(sc["work"], f"page{page}.png")
    if not os.path.exists(p):
        raise HTTPException(404)
    return FileResponse(p, media_type="image/png")


class RenderReq(BaseModel):
    sheet_i: int = 0
    sa_midi: int = 60
    tempo: float = 90
    instrument: str = "flute"
    drone: bool = False
    drone_kind: str = "pa"
    click: bool = False


@app.post("/api/song/{slug}/render")
def render(slug: str, req: RenderReq):
    from bhajan import synth
    sc = _score(slug, req.sheet_i)
    key = _h("render", slug, sc["work"], req.model_dump(), os.path.getmtime(synth.__file__))
    wav = os.path.join(MEDIA, key + ".wav")
    meta = os.path.join(MEDIA, key + ".json")
    with _lock(key):
        if not (os.path.exists(wav) and os.path.exists(meta)):
            y, timings = synth.render(sc["events"], req.sa_midi, req.tempo, instrument=req.instrument,
                                      drone=req.drone, drone_kind=req.drone_kind, click=req.click,
                                      beats_per_bar=sc.get("bar") or 4)
            synth.write_wav(wav, y, 32000)
            with open(meta, "w") as f:
                json.dump({"timings": timings, "duration": len(y) / 32000}, f)
    with open(meta) as f:
        m = json.load(f)
    return {"audio_url": f"/media/{key}.wav", **m}


@app.get("/api/song/{slug}/midi")
def midi(slug: str, sheet_i: int = 0, sa_midi: int = 60, tempo: float = 90):
    from bhajan import synth
    sc = _score(slug, sheet_i)
    key = _h("midi", slug, sc["work"], sa_midi, tempo)
    path = os.path.join(MEDIA, key + ".mid")
    if not os.path.exists(path):
        synth.write_midi(path, sc["events"], sa_midi, tempo)
    return FileResponse(path, media_type="audio/midi", filename=f"{slug}-sa{sa_midi}.mid")


@app.get("/api/song/{slug}/audio/{i}")
def original_audio(slug: str, i: int):
    info = _song(slug)
    if not (0 <= i < len(info["audio"])):
        raise HTTPException(404, "No such recording")
    try:
        path = sr.audio_file(info["audio"][i]["url"])
    except Exception as e:
        raise HTTPException(502, f"Could not download the recording: {e}")
    return FileResponse(path, media_type="audio/mpeg")


@app.post("/api/song/{slug}/analyze")
def analyze(slug: str, audio_i: int = 0):
    info = _song(slug)
    if not (0 <= audio_i < len(info["audio"])):
        raise HTTPException(404, "This bhajan has no recording on Sai Rhythms.")
    cache = os.path.join(MEDIA, _h("key", slug, info["audio"][audio_i]["url"], 2) + ".json")
    if os.path.exists(cache):
        with open(cache) as f:
            return json.load(f)
    path = sr.audio_file(info["audio"][audio_i]["url"])
    hist = None
    if info["sheets"]:
        try:
            hist = _melody_hist(_score(slug, 0))
        except Exception:
            hist = None
    priors = [p["pc"] for p in (info.get("gents_pitch"), info.get("ladies_pitch")) if p]
    res = audio.analyze_key(path, info["raga_aroha"] + info["raga_avaroha"], melody_hist=hist, priors=priors)
    if not res.get("ok"):
        raise HTTPException(422, res.get("error", "Could not analyse the recording"))
    with open(cache, "w") as f:
        json.dump(res, f)
    return res


class ShiftReq(BaseModel):
    audio_i: int = 0
    semitones: float
    formant: bool = True


@app.post("/api/song/{slug}/shift")
def shift(slug: str, req: ShiftReq):
    info = _song(slug)
    if not (0 <= req.audio_i < len(info["audio"])):
        raise HTTPException(404, "No such recording")
    if abs(req.semitones) > 12.5:
        raise HTTPException(400, "Shift must be within an octave")
    src = sr.audio_file(info["audio"][req.audio_i]["url"])
    key = _h("shift", slug, info["audio"][req.audio_i]["url"], round(req.semitones, 3), req.formant)
    dst = os.path.join(MEDIA, key + ".mp3")
    with _lock(key):
        if not os.path.exists(dst):
            audio.pitch_shift(src, dst, req.semitones, formant=req.formant)
    return {"audio_url": f"/media/{key}.mp3"}


@app.middleware("http")
async def _revalidate_static(request, call_next):
    resp = await call_next(request)
    if not request.url.path.startswith("/media/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


app.mount("/media", StaticFiles(directory=MEDIA), name="media")
app.mount("/", StaticFiles(directory=os.path.join(ROOT, "static"), html=True), name="static")


@app.exception_handler(Exception)
async def _err(request, exc):  # pragma: no cover - surface errors as JSON for the UI
    return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=500)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8777"))
    print(f"Bhajan Studio running at http://127.0.0.1:{port}")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
