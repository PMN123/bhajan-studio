"""Bhajan Studio: transpose Sai Rhythms bhajans to your key.

Run:  ./start.sh                      (installs what it needs, opens your browser)
  or  uv run server.py [--open] [--port N] [--host H]

The PORT environment variable sets the default port (8777). If the port is busy, the next free
one (up to +20) is used and printed. The server only listens on this computer (127.0.0.1) unless
--host says otherwise.
"""
from __future__ import annotations

if __name__ == "__main__":  # say something before the heavy imports below
    print("Starting Bhajan Studio...", flush=True)

import argparse
import errno
import hashlib
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import webbrowser
from typing import Literal

import numpy as np
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from bhajan import audio, sheet, store
from bhajan.sairhythms import SaiRhythms, canonical_slug

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("BHAJAN_DATA", os.path.join(ROOT, "data"))
MEDIA = os.path.join(DATA, "media")
MEDIA_CAP = 500 * 1024 * 1024   # rendered/shifted audio kept in data/media; least recently used goes first
SHIFT_VERSION = "pylibrb-r3-limiter-1"   # change when pitch_shift's output changes, to re-shift
KEY_VERSION = 3                          # same for analyze_key's result
os.makedirs(MEDIA, exist_ok=True)

Instrument = Literal["flute", "piano", "harmonium"]

sr = SaiRhythms(DATA)
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


# Only this computer's own browser may use the app: the Host check stops DNS-rebinding pages, and
# the Origin / Sec-Fetch-Site checks stop other websites from driving the API through the browser.
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]"}
_allow_any_host = False   # set by main() when the user explicitly listens on another address


def _host_name(host: str) -> str:
    host = host.strip().lower()
    if host.startswith("["):
        return host[: host.find("]") + 1]
    return host.split(":", 1)[0]


@app.middleware("http")
async def _guard_and_revalidate(request, call_next):
    host = request.headers.get("host", "")
    if not _allow_any_host and _host_name(host) not in _LOCAL_HOSTS:
        return JSONResponse({"detail": "Bhajan Studio only answers requests addressed to 127.0.0.1 or localhost."},
                            status_code=403)
    if request.url.path.startswith("/api/"):
        site = request.headers.get("sec-fetch-site")
        origin = request.headers.get("origin")
        if (site and site not in ("same-origin", "none")) or \
                (origin is not None and urllib.parse.urlsplit(origin).netloc.lower() != host.lower()):
            return JSONResponse({"detail": "Requests from other websites are not allowed."}, status_code=403)
    resp = await call_next(request)
    if not request.url.path.startswith("/media/"):
        resp.headers["Cache-Control"] = "no-cache"
    return resp


app.mount("/media", StaticFiles(directory=MEDIA), name="media")
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

    ap = argparse.ArgumentParser(description="Run the Bhajan Studio web app on this computer.")
    ap.add_argument("--host", default="127.0.0.1",
                    help="address to listen on (default: 127.0.0.1, this computer only). Anything else "
                         "lets other devices on your network use the app, with no password.")
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
                 f"Pick another one, e.g.  ./start.sh --port 9000")

    port = sock.getsockname()[1]
    url = f"http://{_browser_host(args.host)}:{port}"
    notes = []
    if port != args.port:
        notes += [f"Port {args.port} was busy, so this uses {port}.",
                  "(Is Bhajan Studio already open in another window?)"]
    if args.host.strip().lower() not in _LOCAL_HOSTS | {"::1"}:
        _allow_any_host = True
        notes.append(f"WARNING: listening on {args.host or 'all interfaces'}, so other devices on your "
                     "network can use this app too (there is no password).")
    _prune_media()

    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=port, log_level="warning"))
    threading.Thread(target=_when_ready, args=(server, url, notes, args.open), daemon=True).start()
    server.run(sockets=[sock])


if __name__ == "__main__":
    main()
