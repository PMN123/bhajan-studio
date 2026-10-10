"""Client for sairhythms.org: song index, song pages, recordings and sheet music.

Everything fetched is cached under the data directory so the site is only hit
once per song (the index is refreshed weekly).
"""
from __future__ import annotations

import ast
import hashlib
import html
import json
import os
import re
import ssl
import time
import urllib.parse
import urllib.request

try:
    import certifi
    _CTX = ssl.create_default_context(cafile=certifi.where())
except Exception:  # pragma: no cover
    _CTX = ssl.create_default_context()

BASE = "https://sairhythms.org"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) BhajanStudio/1.0"
LETTER_PC = {"C": 0, "C#": 1, "DB": 1, "D": 2, "D#": 3, "EB": 3, "E": 4, "F": 5, "F#": 6, "GB": 6,
             "G": 7, "G#": 8, "AB": 8, "A": 9, "A#": 10, "BB": 10, "B": 11}


def _get(url: str, referer: str | None = None, data: bytes | None = None, timeout=40) -> bytes:
    headers = {"User-Agent": UA}
    if referer:
        headers["Referer"] = referer
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        headers["X-Requested-With"] = "XMLHttpRequest"
    req = urllib.request.Request(url, headers=headers, data=data)
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as r:
        return r.read()


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

    # ------------------------------------------------------------- index
    def index(self) -> list[dict]:
        if self._index is not None:
            return self._index
        path = os.path.join(self.data_dir, "index.json")
        fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < 7 * 86400
        if not fresh:
            try:
                raw = _get(BASE + "/song/signup/api", referer=BASE + "/",
                           data=b"action=load_resource&resource=search", timeout=90)
                songs = json.loads(json.loads(raw)["data"])
                slim = [self._slim(s) for s in songs]
                with open(path, "w") as f:
                    json.dump(slim, f)
            except Exception:
                if not os.path.exists(path):
                    raise
        with open(path) as f:
            self._index = json.load(f)
        return self._index

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
            "slug": s["url"].rsplit("/", 1)[-1],
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
        safe = hashlib.sha1(slug.encode()).hexdigest()[:16]
        path = os.path.join(self.data_dir, "songs", safe + ".json")
        if os.path.exists(path) and not refresh:
            with open(path) as f:
                return json.load(f)
        page = _get(BASE + "/song/" + urllib.parse.quote(slug)).decode("utf-8", "replace")
        info = parse_song_page(page)
        info["slug"] = slug
        info["url"] = BASE + "/song/" + slug
        with open(path, "w") as f:
            json.dump(info, f)
        return info

    def _cached_download(self, url: str, sub: str, ext: str, referer=None) -> str:
        key = hashlib.sha1(url.encode()).hexdigest()[:20]
        path = os.path.join(self.data_dir, sub, key + ext)
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            data = _get(url, referer=referer, timeout=120)
            tmp = path + ".part"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        return path

    def audio_file(self, url: str) -> str:
        if url.startswith("/"):
            url = BASE + url
        return self._cached_download(_quote_path(url), "audio", ".mp3", referer=BASE + "/")

    def sheet_pdf(self, api_path: str) -> str:
        viewer = BASE + "/sites/default/files/pages/pdfjs/web/viewer.html?file=" + urllib.parse.quote(api_path, safe="")
        data_url = BASE + api_path
        path = self._cached_download(data_url, "sheets", ".pdf", referer=viewer)
        with open(path, "rb") as f:
            head = f.read(5)
        if head != b"%PDF-":
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
