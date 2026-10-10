"""Client for sairhythms.org: song index, song pages, recordings and sheet music.

Everything fetched is cached under the data directory so the site is only hit
once in a while: the index is refreshed weekly and each song page monthly.
"""
from __future__ import annotations

import ast
import gzip
import hashlib
import html
import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import store

try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover
    _CTX = ssl.create_default_context()

BASE = "https://sairhythms.org"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) BhajanStudio/1.0"
INDEX_TTL = 7 * 86400
SONG_TTL = 30 * 86400
PARSER_VERSION = 1  # bump when parse_song_page changes, so cached song pages are re-read
# characters left as they are when a sheet URL taken from a page is re-quoted
_URL_SAFE = "/?=&+%:,;@!$'()*~"
LETTER_PC = {"C": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3, "E": 4, "F": 5, "F#": 6, "GB": 6,
             "G": 7, "G#": 8, "AB": 8, "A": 9, "A#": 10, "BB": 10, "B": 11}


# Be gentle with sairhythms.org: at most two requests in flight and a short gap between them.
_SITE_SLOTS = threading.BoundedSemaphore(2)
_SITE_GAP = 0.3
_site_last = [0.0]
_site_lock = threading.Lock()
SNAPSHOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "song_index.json.gz")


def _get(url: str, referer: str | None = None, data: bytes | None = None, timeout=40) -> bytes:
    if urllib.parse.urlsplit(url).netloc.endswith("sairhythms.org"):
        with _SITE_SLOTS:
            with _site_lock:
                wait = _site_last[0] + _SITE_GAP - time.monotonic()
                if wait > 0:
                    time.sleep(wait)
                _site_last[0] = time.monotonic()
            return _fetch(url, referer, data, timeout)
    return _fetch(url, referer, data, timeout)


def _fetch(url: str, referer: str | None, data: bytes | None, timeout) -> bytes:
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["X-Requested-With"] = "XMLHttpRequest"
    req = urllib.request.Request(url, headers=headers, data=data)
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return r.read()


def canonical_slug(slug: str) -> str:
    """Song slugs appear both percent-encoded ('se%C3%B1or-del-sol') and decoded ('señor-del-sol');
    the decoded form is the one used for caching and in the app's URLs."""
    return urllib.parse.unquote(slug)


def _quote_path(url: str) -> str:
    """Percent-encode the path of a URL (file names can contain spaces or unicode)."""
    p = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, urllib.parse.quote(p.path, safe="/%"), p.query, p.fragment))


class SaiRhythms:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        for sub in ("songs", "audio", "sheets"):
            os.makedirs(os.path.join(data_dir, sub), exist_ok=True)
        self._index = None
        self._index_lock = threading.Lock()

    # ------------------------------------------------------------- index
    def index(self) -> list[dict]:
        if self._index is not None:
            return self._index
        path = os.path.join(self.data_dir, "index.json")
        with self._index_lock:
            if self._index is not None:
                return self._index
            idx = store.read_json(path)
            if idx is None:
                idx = self._seed_index(path)
            if idx is None:
                idx = self._download_index(path)        # no snapshot either: wait for the site
            elif time.time() - os.path.getmtime(path) >= INDEX_TTL:
                # stale: keep serving the old index and refresh it in the background
                threading.Thread(target=self._refresh_index, args=(path,), daemon=True).start()
            self._index = self._canon_index(idx)
        return self._index

    def _seed_index(self, path: str) -> list[dict] | None:
        """First run: start from the song list shipped with the app instead of downloading it.
        Its date is kept as the file time, so the weekly refresh happens in the background."""
        try:
            with gzip.open(SNAPSHOT, "rt", encoding="utf-8") as f:
                snap = json.load(f)
        except Exception:
            return None
        store.write_json(path, snap["songs"])
        os.utime(path, (snap["generated"], snap["generated"]))
        return snap["songs"]

    def _download_index(self, path: str) -> list[dict]:
        raw = _get(BASE + "/song/signup/api", referer=BASE + "/",
                   data=b"action=load_resource&resource=search", timeout=90)
        songs = json.loads(json.loads(raw)["data"])
        slim = [self._slim(s) for s in songs]
        store.write_json(path, slim)
        return slim

    def _refresh_index(self, path: str) -> None:
        try:
            idx = self._download_index(path)
        except Exception:
            return  # offline or the site is down: keep the old index
        self._index = self._canon_index(idx)

    @staticmethod
    def _canon_index(idx: list[dict]) -> list[dict]:
        for s in idx:
            s["slug"] = canonical_slug(s["slug"])
        return idx

    @staticmethod
    def _urls(v) -> list[str]:
        if not v:
            return []
        if isinstance(v, list):
            return [str(x) for x in v]
        try:
            out = ast.literal_eval(v)
            if isinstance(out, list):
                return [str(x) for x in out]
        except Exception:
            pass
        return re.findall(r"https?://[^'\"\s,\]]+", str(v))

    def _slim(self, s: dict) -> dict:
        tags = [t.strip() for t in (s.get("songtags") or "").split(",") if t.strip()]
        return {
            "slug": canonical_slug(s["url"].rsplit("/", 1)[-1]),
            "title": s.get("title", "").strip(),
            "title2": (s.get("title2") or "").strip(),
            "raga": (s.get("raga") or "").strip(),
            "beat": (s.get("beat") or "").strip(),
            "deity": (s.get("deity") or "").strip(),
            "language": (s.get("language") or "").strip(),
            "tempo": (s.get("tempo") or "").strip(),
            "level": (s.get("level") or "").strip(),
            "first_line": (s.get("lyrics") or "").split("\n")[0][:80],
            "audio": len(self._urls(s.get("audio_link"))),
            "sheet": "sheet-music" in tags,
            "tags": tags,
        }

    def search(self, q: str, limit=40) -> list[dict]:
        q = q.strip().lower()
        idx = self.index()
        if not q:
            return idx[:limit]
        words = q.split()
        scored = []
        for s in idx:
            hay = (s["title"] + " " + s["title2"] + " " + s["first_line"]).lower()
            if all(w in hay for w in words):
                score = (0 if s["title"].lower().startswith(q) else 1 if q in s["title"].lower() else 2,
                         0 if s["sheet"] else 1, 0 if s["audio"] else 1, len(s["title"]))
                scored.append((score, s))
        scored.sort(key=lambda x: x[0])
        return [s for _, s in scored[:limit]]

    # ------------------------------------------------------------- songs
    def song(self, slug: str, refresh=False) -> dict:
        slug = canonical_slug(slug)
        safe = hashlib.sha1(slug.encode()).hexdigest()[:16]
        path = os.path.join(self.data_dir, "songs", safe + ".json")
        with store.lock(path):
            cached = store.read_json(path)
            if cached is not None and not refresh and cached.get("parser") == PARSER_VERSION \
                    and time.time() - cached.get("fetched_at", 0) < SONG_TTL:
                return cached | {"slug": slug}
            url = BASE + "/song/" + urllib.parse.quote(slug, safe="")
            try:
                page = _get(url).decode("utf-8", "replace")
            except Exception:
                if cached is not None:      # offline: an old copy is better than nothing
                    return cached | {"slug": slug}
                raise
            info = parse_song_page(page)
            info["slug"] = slug
            info["url"] = url
            info["parser"] = PARSER_VERSION
            info["fetched_at"] = int(time.time())
            store.write_json(path, info)
            return info

    def _cached_download(self, url: str, sub: str, ext: str, referer=None) -> str:
        key = hashlib.sha1(url.encode()).hexdigest()[:20]
        path = os.path.join(self.data_dir, sub, key + ext)
        with store.lock(path):
            if not os.path.exists(path) or os.path.getsize(path) == 0:
                store.write_bytes(path, _get(url, referer=referer, timeout=120))
        return path

    def audio_file(self, url: str) -> str:
        if url.startswith("/"):
            url = BASE + url
        return self._cached_download(_quote_path(url), "audio", ".mp3", referer=BASE + "/")

    def sheet_pdf(self, api_path: str) -> str:
        viewer = BASE + "/sites/default/files/pages/pdfjs/web/viewer.html?file=" + urllib.parse.quote(api_path, safe="")
        # the path was unquoted when the page was parsed; titles can contain spaces, dashes or accents
        data_url = BASE + urllib.parse.quote(api_path, safe=_URL_SAFE)
        path = self._cached_download(data_url, "sheets", ".pdf", referer=viewer)
        with open(path, "rb") as f:
            head = f.read(5)
        if head != b"%PDF-":
            with store.lock(path):
                if os.path.exists(path):
                    os.remove(path)
            raise RuntimeError("Sai Rhythms did not return a PDF for this sheet")
        return path


# ----------------------------------------------------------------- parsing
def _text(fragment: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", fragment)
    t = re.sub(r"<[^>]+>", "", t)
    return html.unescape(t).replace("\xa0", " ").strip()


def _row(page: str, label: str) -> str | None:
    m = re.search(r"<div class='col[^']*strong'>\s*" + re.escape(label) +
                  r"\s*</div>\s*<div class='col[^']*'>(.*?)</div>\s*</div>", page, re.S)
    return m.group(1) if m else None


def parse_pitch(s: str | None) -> dict | None:
    """'1.5 Pancham / C#' -> {kattai: 1.5, shruti: 'Pancham', letter: 'C#', pc: 1}."""
    if not s:
        return None
    m = re.match(r"\s*([\d.]+)\s+(\w+)\s*/\s*([A-G][#b♯♭]?)", _text(s))
    if not m:
        return None
    letter = m.group(3).replace("♯", "#").replace("♭", "b")
    return {"kattai": float(m.group(1)), "shruti": m.group(2), "letter": letter,
            "pc": LETTER_PC.get(letter.upper())}


def parse_song_page(page: str) -> dict:
    info: dict = {}
    m = re.search(r"<title>(.*?)\|", page, re.S)
    info["title"] = html.unescape(m.group(1)).strip() if m else ""
    lines = []
    start = page.find("<div class='lyrics-set'")
    if start >= 0:
        end = page.find("<div class='lyrics-set'", start + 10)
        chunk = page[start:end if end > 0 else start + 20000]
        lines = [_text(x) for x in re.findall(r"<div class='song-(?:first-)?line'>(.*?)</div>", chunk, re.S)]
    info["lyrics"] = [l for l in lines if l]
    m = re.search(r"<div class='devotional-song-meaning[^']*'>(.*?)</div>", page, re.S)
    info["meaning"] = _text(m.group(1)) if m else ""

    raga = re.search(r"<div class='raga-title'>(.*?)</div>", page, re.S)
    info["raga"] = _text(raga.group(1)) if raga else ""
    panel = re.search(r"raga-notes-panel'[^>]*>(.*?)<div class='small-font strong'>In the Key", page, re.S)
    scale = []
    if panel:
        for d in re.findall(r"<div>(.*?)</div>", panel.group(1), re.S):
            toks = _text(d).split()
            if toks and all(re.match(r"^[SRGMPDNsrgmpdn][12]?\+?$", t) for t in toks):
                scale.append(toks)
    info["raga_aroha"] = scale[0] if scale else []
    info["raga_avaroha"] = scale[1] if len(scale) > 1 else []

    for key, label in (("beat", "Beat"), ("tempo", "Tempo"), ("level", "Level"),
                       ("deity", "Deity"), ("language", "Language")):
        v = _row(page, label)
        info[key] = _text(v) if v else ""
    info["gents_pitch"] = parse_pitch(_row(page, "Reference Gents Pitch"))
    info["ladies_pitch"] = parse_pitch(_row(page, "Reference Ladies Pitch"))
    rng = _row(page, "Notes Range") or ""
    lo = re.search(r"Lowest Note:\s*([^/<]+?)\s*/", rng)
    hi = re.search(r"Highest Note:\s*([^/<]+?)\s*/", rng)
    info["range"] = {"low": _text(lo.group(1)) if lo else None, "high": _text(hi.group(1)) if hi else None}

    audio_block = re.search(r"audio-row'>(.*?)(?:<div class='row devotional-song-meta-data-grid (?!audio)|$)", page, re.S)
    srcs = re.findall(r"<source src='([^']+)'", audio_block.group(1)) if audio_block else []
    seen, audio = set(), []
    for s in srcs:
        u = s if s.startswith("http") else BASE + s
        if u not in seen:
            seen.add(u)
            audio.append({"url": u, "name": urllib.parse.unquote(u.rsplit("/", 1)[-1])})
    info["audio"] = audio
    sheets = []
    for f in re.findall(r"viewer\.html\?file=([^'\"]+)", page):
        p = urllib.parse.unquote(html.unescape(f))
        if p not in sheets:
            sheets.append(p)
    info["sheets"] = sheets
    info["videos"] = sorted(set(re.findall(r"https://www\.youtube\.com/watch\?v=[\w-]+", page)))
    return info
