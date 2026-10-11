"""Optical music recognition tuned for Sai Samarpan sheet music.

The sheets on Sai Rhythms are single-voice treble-clef melodies engraved the
same way (MuseScore, key of C, Sa = C4), with a sargam row and a Western-letter
row printed above every note and blue lyrics below.  This module reads one
rendered page at a time:

  * staff lines, barlines and note heads come from image analysis, so pitch is
    taken from the staff position plus any accidental glyph;
  * stems, beams, flags, dots, ties and rests give the rhythm;
  * Vision OCR (macOS) supplies the printed labels, lyrics and tempo, which are
    used to confirm or correct pitches.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field, asdict
from types import SimpleNamespace

import numpy as np
from scipy import ndimage as ndi

DIATONIC = [0, 2, 4, 5, 7, 9, 11]
LETTERS = "CDEFGAB"
SARGAM = {0: "S", 1: "R1", 2: "R2", 3: "G1", 4: "G2", 5: "M1", 6: "M2",
          7: "P", 8: "D1", 9: "D2", 10: "N1", 11: "N2"}
SARGAM_PC = {v: k for k, v in SARGAM.items()}
SARGAM_PC.update({"R": 2, "G": 4, "M": 5, "D": 9, "N": 11})


def sargam_name(midi: int, sa: int = 60) -> str:
    """Sargam with octave marks used on the sheets: lowercase = lower, '+' = upper."""
    rel = midi - sa
    name = SARGAM[rel % 12]
    octv = rel // 12
    if octv < 0:
        return name.lower() + ("-" * (-octv - 1))
    return name + "+" * octv


# --------------------------------------------------------------------------- OCR
# Printed sargam labels, lyrics and the tempo mark are read with Apple's Vision
# framework, called in-process through PyObjC (no compiler or Homebrew needed).
# BHAJAN_OCR=auto|vision|tesseract|none picks the engine; auto = Vision when it
# can be imported (macOS), else tesseract if it is installed, else no OCR (the OMR
# still works from the notation alone, just without label/lyric/tempo hints).
OCR_ENGINES = ("auto", "vision", "tesseract", "none")
_VISION = None          # namespace of PyObjC symbols once resolved; False if unavailable
_VISION_LOCK = threading.Lock()
_log = logging.getLogger(__name__)


def _vision_api():
    """The PyObjC symbols vision_ocr needs, resolved once under a lock: PyObjC's lazy
    framework modules are not safe to populate from several threads at once (a
    concurrent first lookup raised KeyError inside uvicorn's thread pool)."""
    global _VISION
    with _VISION_LOCK:
        if _VISION is None:
            try:
                import objc
                import Quartz
                import Vision
                _VISION = SimpleNamespace(
                    autorelease_pool=objc.autorelease_pool,
                    url_for_path=Quartz.CFURLCreateFromFileSystemRepresentation,
                    image_source=Quartz.CGImageSourceCreateWithURL,
                    image_at=Quartz.CGImageSourceCreateImageAtIndex,
                    image_w=Quartz.CGImageGetWidth, image_h=Quartz.CGImageGetHeight,
                    min_x=Quartz.CGRectGetMinX, max_y=Quartz.CGRectGetMaxY,
                    rect_w=Quartz.CGRectGetWidth, rect_h=Quartz.CGRectGetHeight,
                    TextRequest=Vision.VNRecognizeTextRequest,
                    Handler=Vision.VNImageRequestHandler,
                    accurate=Vision.VNRequestTextRecognitionLevelAccurate)
            except Exception:
                _VISION = False
    return _VISION or None


def _tesseract() -> str | None:
    """The tesseract program: on PATH, or where its Windows installer puts it (not on PATH)."""
    exe = shutil.which("tesseract")
    if exe is None and os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"),
                     os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
            p = os.path.join(base or "", "Tesseract-OCR", "tesseract.exe")
            if base and os.path.exists(p):
                return p
    return exe


def _ocr_choice(engine: str | None) -> str:
    eng = (engine or os.environ.get("BHAJAN_OCR") or "auto").strip().lower()
    if eng not in OCR_ENGINES:
        raise ValueError(f"OCR engine must be one of {'/'.join(OCR_ENGINES)}, not {eng!r}")
    return eng


def ocr_engine(engine: str | None = None) -> str:
    """The engine run_ocr will use: 'vision', 'tesseract' or 'none'."""
    eng = _ocr_choice(engine)
    if eng != "auto":
        return eng
    if _vision_api():
        return "vision"
    return "tesseract" if _tesseract() else "none"


def run_ocr(png_path: str, engine: str | None = None) -> list[dict]:
    """OCR an image -> tokens {t, x, y, w, h, c[, line]}: pixel box (top-left origin),
    confidence 0..1, and for Vision the whole recognised line the token came from."""
    choice = _ocr_choice(engine)
    eng = ocr_engine(choice)
    if eng == "vision":
        try:
            return vision_ocr(png_path)
        except Exception as e:
            if choice != "auto":
                raise
            _log.warning("Vision OCR failed on %s (%s); trying tesseract", png_path, e)
            return tesseract_ocr(png_path)
    if eng == "tesseract":
        if choice != "auto" and not _tesseract():
            raise RuntimeError("BHAJAN_OCR=tesseract but tesseract is not installed (or not on PATH)")
        return tesseract_ocr(png_path)
    return []


def _utf16_len(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def vision_ocr(png_path: str) -> list[dict]:
    """Apple Vision text recognition (accurate, no language correction), one token per
    whitespace-separated word with its own box.  Thread-safe; macOS only."""
    v = _vision_api()
    if not v:
        raise RuntimeError("Vision OCR needs macOS and pyobjc-framework-Vision/-Quartz")
    out = []
    with v.autorelease_pool():
        path = os.fsencode(os.path.abspath(png_path))
        src = v.image_source(v.url_for_path(None, path, len(path), False), None)
        img = v.image_at(src, 0, None) if src is not None else None
        if img is None:
            raise RuntimeError(f"cannot read image {png_path}")
        W, H = float(v.image_w(img)), float(v.image_h(img))
        req = v.TextRequest.alloc().init()
        req.setRecognitionLevel_(v.accurate)
        req.setUsesLanguageCorrection_(False)
        req.setMinimumTextHeight_(0.004)
        handler = v.Handler.alloc().initWithCGImage_options_(img, {})
        ok, err = handler.performRequests_error_([req], None)
        if not ok:
            raise RuntimeError(f"Vision OCR failed: {err}")
        for obs in req.results() or []:
            cands = obs.topCandidates_(1)
            if not cands:
                continue
            cand = cands[0]
            line = str(cand.string())
            conf = float(cand.confidence())
            for m in re.finditer(r"\S+", line):
                rng = (_utf16_len(line[:m.start()]), _utf16_len(m.group()))
                box, _ = cand.boundingBoxForRange_error_(rng, None)
                if box is None:
                    continue
                bb = box.boundingBox()          # normalised, bottom-left origin
                out.append({"t": m.group(),
                            "x": v.min_x(bb) * W,
                            "y": (1 - v.max_y(bb)) * H,
                            "w": v.rect_w(bb) * W,
                            "h": v.rect_h(bb) * H,
                            "c": conf, "line": line})
    return out


def tesseract_ocr(png_path: str) -> list[dict]:
    try:
        r = subprocess.run([_tesseract() or "tesseract", png_path, "-", "--psm", "11", "tsv"],
                           capture_output=True, timeout=120, encoding="utf-8", errors="replace")
    except Exception:
        return []
    out = []
    for line in r.stdout.splitlines()[1:]:
        p = line.split("\t")
        if len(p) < 12 or not p[11].strip():
            continue
        out.append({"t": p[11].strip(), "x": float(p[6]), "y": float(p[7]), "w": float(p[8]),
                    "h": float(p[9]), "c": float(p[10]) / 100})
    return out


# ------------------------------------------------------------------ data types
@dataclass
class Staff:
    lines: list[float]            # y of the 5 lines, top to bottom
    x0: int
    x1: int
    sp: float                     # line spacing
    barlines: list[int] = field(default_factory=list)

    @property
    def top(self):
        return self.lines[0]

    @property
    def bottom(self):
        return self.lines[4]


@dataclass
class Event:
    kind: str                     # "note" | "rest"
    dur: float                    # quarter-note units
    x: float
    y: float
    staff: int
    page: int
    midi: int | None = None
    step: int | None = None       # diatonic staff step (0 = E4 bottom line)
    acc: int | None = None        # explicit accidental -1/0/+1
    dots: int = 0
    tie_next: bool = False
    grace: bool = False
    lyric: str = ""
    label: str = ""              # printed sargam label, if OCR found it
    wlabel: str = ""             # printed western label
    measure: int = 0
    bbox: list = field(default_factory=list)
    flags: int = 0
    hollow: bool = False
    stem: int = 0                 # +1 up, -1 down, 0 none
    conf: float = 1.0
    notes: list = field(default_factory=list)


# ------------------------------------------------------------- image helpers
def masks(rgb: np.ndarray):
    a = rgb.astype(np.int16)
    g = a.mean(2)
    chroma = a.max(2) - a.min(2)
    black = (g < 165) & (chroma < 80)
    blue = (a[:, :, 2] - a[:, :, 0] > 40) & (g < 190)
    red = (a[:, :, 0] - a[:, :, 2] > 40) & (g < 190)
    return black, blue, red


def runs(v: np.ndarray):
    """Start/end (exclusive) of True runs in a 1-D bool array."""
    d = np.diff(np.concatenate([[0], v.astype(np.int8), [0]]))
    return np.where(d == 1)[0], np.where(d == -1)[0]


def find_staves(black: np.ndarray) -> list[Staff]:
    H, W = black.shape
    # staff lines are thin: drop anything thicker (beams, heads) before the row profile
    thick = ndi.binary_opening(black, structure=np.ones((5, 1), bool))
    rf = (black & ~thick).mean(1)
    st, en = runs(rf > 0.25)
    lines = [((s + e - 1) / 2, s, e) for s, e in zip(st, en)]
    staves = []
    i = 0
    while i + 4 < len(lines):
        ys = [l[0] for l in lines[i:i + 5]]
        d = np.diff(ys)
        if d.min() > 6 and d.max() / d.min() < 1.35 and d.max() < 60:
            sp = float(np.mean(d))
            mid = int(round(ys[2]))
            row = black[mid - 1:mid + 2].any(0)
            s2, e2 = runs(row)
            k = np.argmax(e2 - s2)
            staves.append(Staff(lines=ys, x0=int(s2[k]), x1=int(e2[k] - 1), sp=sp))
            i += 5
        else:
            i += 1
    return staves


def remove_staff_lines(black: np.ndarray, staves: list[Staff]) -> np.ndarray:
    out = black.copy()
    H = black.shape[0]
    for s in staves:
        for y in s.lines:
            y0, y1 = int(np.floor(y - 1.2)), int(np.ceil(y + 1.2))
            band = black[y0:y1 + 1, s.x0:s.x1 + 1]
            # run length above/below the band
            up = np.zeros(band.shape[1], int)
            dn = np.zeros(band.shape[1], int)
            for k in range(1, 4):
                if y0 - k >= 0:
                    up += (black[y0 - k, s.x0:s.x1 + 1] & (up == k - 1))
                if y1 + k < H:
                    dn += (black[y1 + k, s.x0:s.x1 + 1] & (dn == k - 1))
            clear = (up + dn) <= 1
            sub = out[y0:y1 + 1, s.x0:s.x1 + 1]
            sub[:, clear] = False
    return out


def ellipse_se(w: float, h: float):
    rw, rh = w / 2, h / 2
    yy, xx = np.mgrid[-int(rh):int(rh) + 1, -int(rw):int(rw) + 1]
    return (xx / max(rw, 0.5)) ** 2 + (yy / max(rh, 0.5)) ** 2 <= 1.0


# ---------------------------------------------------------- symbol detectors
def detect_filled_heads(nol: np.ndarray, staff: Staff, x_start: int):
    sp = staff.sp
    y0 = int(staff.top - 7 * sp)
    y1 = int(staff.bottom + 7 * sp)
    y0 = max(0, y0)
    region = nol[y0:y1, x_start:staff.x1 + 1]
    opened = ndi.binary_opening(region, structure=ellipse_se(0.78 * sp, 0.66 * sp))
    lab, n = ndi.label(opened)
    heads = []
    for i, sl in enumerate(ndi.find_objects(lab)):
        if sl is None:
            continue
        hh = sl[0].stop - sl[0].start
        ww = sl[1].stop - sl[1].start
        area = (lab[sl] == i + 1).sum()
        if not (0.75 * sp <= hh <= 1.5 * sp and 0.8 * sp <= ww <= 1.9 * sp):
            continue
        if area < 0.8 * sp * sp:
            continue
        cy = (sl[0].start + sl[0].stop - 1) / 2 + y0
        cx = (sl[1].start + sl[1].stop - 1) / 2 + x_start
        heads.append(dict(cx=cx, cy=cy, x0=sl[1].start + x_start, x1=sl[1].stop - 1 + x_start,
                          y0=sl[0].start + y0, y1=sl[0].stop - 1 + y0, w=ww, h=hh, area=area,
                          hollow=False, small=ww < 1.0 * sp))
    return heads


def vertical_segments(nol: np.ndarray, staff: Staff, x_start: int, min_len: float):
    """Thin vertical strokes (stems, barlines) in the staff's neighbourhood."""
    sp = staff.sp
    y0 = max(0, int(staff.top - 8 * sp))
    y1 = min(nol.shape[0], int(staff.bottom + 8 * sp))
    region = nol[y0:y1, x_start:staff.x1 + 1]
    # keep pixels that belong to long vertical runs
    min_px = int(min_len)
    segs = []
    for x in range(region.shape[1]):
        s, e = runs(region[:, x])
        for a, b in zip(s, e):
            if b - a >= min_px:
                segs.append((x + x_start, a + y0, b - 1 + y0))
    # merge adjacent columns with overlapping spans
    segs.sort()
    merged = []
    for x, a, b in segs:
        for m in merged[-6:]:
            if x - m["xr"] <= 1 and min(b, m["y1"]) - max(a, m["y0"]) > 0.6 * min(b - a, m["y1"] - m["y0"]):
                m["xr"] = x
                m["y0"] = min(m["y0"], a)
                m["y1"] = max(m["y1"], b)
                m["cols"] += 1
                break
        else:
            merged.append(dict(xl=x, xr=x, y0=a, y1=b, cols=1))
    for m in merged:
        m["x"] = (m["xl"] + m["xr"]) / 2
        m["w"] = m["xr"] - m["xl"] + 1
    return merged


def staff_step(staff: Staff, y: float) -> int:
    return int(round((staff.bottom - y) / (staff.sp / 2)))


def step_to_midi(step: int, acc: int = 0) -> int:
    idx = 4 * 7 + 2 + step          # E4 is diatonic index 30
    octave, letter = divmod(idx, 7)
    return 12 * (octave + 1) + DIATONIC[letter] + acc


def step_letter(step: int) -> str:
    return LETTERS[(30 + step) % 7]


# -------------------------------------------------------------- page reading
SARGAM_RE = re.compile(r"^([SRGMPDNsrgmpdn])([12]?)(\+*)$")
WEST_RE = re.compile(r"^([A-G])\s*([b♭#♯]?)$")


def norm_token(t: str) -> str:
    return (t.replace("♭", "b").replace("♯", "#").replace("’", "").replace("'", "")
            .replace("|", "1").replace("l", "1") if len(t) <= 4 else t)


def classify_tokens(tokens):
    """Split OCR tokens into sargam labels, western labels, other black text."""
    sarg, west = [], []
    # merge 'B' + 'b' / '♭' split tokens
    toks = sorted(tokens, key=lambda o: (round(o["y"] / 8), o["x"]))
    merged = []
    for o in toks:
        t = o["t"].strip()
        if merged and t in ("b", "♭", "#", "♯") and abs(merged[-1]["y"] - o["y"]) < 0.8 * o["h"] \
                and 0 <= o["x"] - (merged[-1]["x"] + merged[-1]["w"]) < 1.2 * o["h"] \
                and re.match(r"^[A-G]$", merged[-1]["t"]):
            m = merged[-1]
            m["t"] = m["t"] + t
            m["w"] = o["x"] + o["w"] - m["x"]
            continue
        merged.append(dict(o, t=t))
    for o in merged:
        t = o["t"]
        if re.match(r"^[SRGMPDNsrgmpdn][12IlI]?[t\*]$", t):
            t = t[:-1] + "+"
        tn = t.replace("♭", "b").replace("♯", "#")
        if WEST_RE.match(tn):
            west.append(dict(o, t=tn))
            continue
        m = SARGAM_RE.match(t.replace("l", "1").replace("I", "1") if len(t) <= 4 else t)
        if m and (m.group(2) or m.group(1).upper() in "SP"):
            sarg.append(dict(o, t=m.group(0)))
    return sarg, west


def sargam_label_to_midi(t: str):
    m = SARGAM_RE.match(t)
    if not m:
        return None
    letter, num, plus = m.groups()
    key = letter.upper() + num
    if key not in SARGAM_PC:
        return None
    pc = SARGAM_PC[key]
    octv = len(plus) - (1 if letter.islower() else 0)
    return 60 + pc + 12 * octv


def west_to_pc(t: str):
    m = WEST_RE.match(t)
    if not m:
        return None
    pc = DIATONIC[LETTERS.index(m.group(1))]
    if m.group(2) in ("b", "♭"):
        pc -= 1
    elif m.group(2) in ("#", "♯"):
        pc += 1
    return pc % 12


def read_page(rgb: np.ndarray, page_index: int, ocr_tokens: list[dict] | None = None, debug=False):
    """Read one rendered page. Returns staves, events (notes/rests), tempo, warnings."""
    black, blue, red = masks(rgb)
    staves = find_staves(black)
    result = {"staves": [], "events": [], "tempo": None, "warnings": []}
    if not staves:
        return result
    sp = float(np.median([s.sp for s in staves]))
    tokens = ocr_tokens or []
    sarg, west = classify_tokens(tokens)

    # Mask black label text so it doesn't confuse rhythm detection.
    text_mask = text_glyph_mask(black, tokens, staves)
    clean = black & ~text_mask
    nol = remove_staff_lines(clean, staves)

    # tempo: number next to the red quarter-note mark
    if red.any():
        ys, xs = np.where(red)
        for o in tokens:
            cx, cy = o["x"] + o["w"] / 2, o["y"] + o["h"] / 2
            if xs.min() - 5 <= cx <= xs.max() + 5 and ys.min() - 5 <= cy <= ys.max() + 5:
                m = re.search(r"(\d{2,3})", o["t"])
                if m:
                    result["tempo"] = int(m.group(1))

    lyric_tokens = []
    for o in tokens:
        x0, y0 = int(o["x"]), int(o["y"])
        x1, y1 = int(o["x"] + o["w"]), int(o["y"] + o["h"])
        box = blue[max(0, y0):y1 + 1, max(0, x0):x1 + 1]
        if box.size and box.mean() > 0.04:
            lyric_tokens.append(o)

    events: list[Event] = []
    for si, st in enumerate(staves):
        sp = st.sp
        segs = vertical_segments(nol, st, st.x0, 2.0 * sp)
        x_start = music_start(nol, st, time_sig=(si == 0 and page_index == 0))
        heads = detect_filled_heads(nol, st, x_start)
        heads = [h for h in heads if h["cx"] > x_start + 0.3 * sp]

        # ---- barlines: vertical strokes spanning exactly the staff
        bars = []
        for s in segs:
            if s["w"] > 0.7 * sp:
                continue
            if abs(s["y0"] - st.top) <= 0.3 * sp and abs(s["y1"] - st.bottom) <= 0.3 * sp:
                near_head = any(abs(h["cx"] - s["x"]) < 1.0 * sp for h in heads)
                hollow_end = False
                for hy, side in ((s["y1"], -1), (s["y0"], 1)):
                    c = probe_hollow(clean, s["x"] + side * 0.62 * sp, hy, sp)
                    if c and abs(c["cy"] - hy) < 0.55 * sp and ring_ok(clean, c, sp):
                        hollow_end = True
                if not near_head and not hollow_end:
                    bars.append(s["x"])
        bars = sorted(bars)
        dedup = []
        for b in bars:
            if dedup and b - dedup[-1] < 0.9 * sp:
                continue
            dedup.append(b)
        st.barlines = dedup

        # ---- stems: attach to heads
        stems = [s for s in segs if s["w"] <= max(4, 0.3 * sp) and (s["y1"] - s["y0"]) >= 2.2 * sp]
        for h in heads:
            best = None
            for s in stems:
                # up-stem on right edge, bottom near head centre
                if abs(s["x"] - h["x1"]) <= 0.3 * sp and h["y0"] - 0.2 * sp <= s["y1"] <= h["y1"] + 0.3 * sp and s["y0"] < h["cy"] - 2.3 * sp:
                    cand = (1, s)
                elif abs(s["x"] - h["x0"]) <= 0.3 * sp and h["y0"] - 0.3 * sp <= s["y0"] <= h["y1"] + 0.2 * sp and s["y1"] > h["cy"] + 2.3 * sp:
                    cand = (-1, s)
                else:
                    continue
                if best is None or (cand[1]["y1"] - cand[1]["y0"]) > (best[1]["y1"] - best[1]["y0"]):
                    best = cand
            if best:
                h["stem"], h["stem_seg"] = best
            else:
                h["stem"], h["stem_seg"] = 0, None

        heads = [h for h in heads if h["stem"] != 0]

        # ---- hollow heads: stems with nothing filled at the head end
        used = {id(h["stem_seg"]) for h in heads if h.get("stem_seg")}
        for s in stems:
            if id(s) in used or s["x"] < x_start:
                continue
            if any(abs(s["x"] - b) < 0.3 * sp for b in st.barlines):
                continue  # barline
            for direction, hy, side in ((1, s["y1"], -1), (-1, s["y0"], 1)):
                hx = s["x"] + side * 0.62 * sp
                h = probe_hollow(clean, hx, hy - direction * 0.05 * sp, sp)
                if h and (abs(h["cx"] - hx) > 0.4 * sp or abs(h["cy"] - hy) > 0.55 * sp):
                    h = None
                if h and any(0 <= f["x0"] - h["cx"] <= 2.4 * sp and abs(f["cy"] - h["cy"]) < 1.6 * sp
                             for f in heads if not f["hollow"]):
                    h = None
                if h and not ring_ok(clean, h, sp):
                    h = None
                if h:
                    h.update(stem=direction, stem_seg=s, hollow=True)
                    heads.append(h)
                    break
        # whole notes: hollow heads with no stem (search spaces/lines between notes)
        heads.extend(find_whole_notes(clean, nol, st, heads, x_start))

        heads.sort(key=lambda h: h["cx"])
        for h in heads:
            h["cy"] = st.bottom - staff_step(st, h["cy"]) * sp / 2

        # ---- rhythm for each head
        for h in heads:
            step = staff_step(st, h["cy"])
            flags = 0
            if h["stem"] and h.get("stem_seg"):
                flags = count_beams(nol, h["stem_seg"], h["stem"], sp)
            dots = count_dots(nol, h, st)
            if h["hollow"]:
                base = 2.0 if h["stem"] else 4.0
            else:
                base = 1.0 / (2 ** flags)
            dur = base * (1.5 if dots == 1 else 1.75 if dots >= 2 else 1.0)
            acc = detect_accidental(nol, h, st)
            ev = Event(kind="note", dur=dur, x=h["cx"], y=h["cy"], staff=si, page=page_index,
                       step=step, acc=acc, dots=dots, flags=flags, hollow=h["hollow"],
                       stem=h["stem"], grace=bool(h.get("small")) and not h["hollow"],
                       bbox=[int(h["x0"]), int(h["y0"]), int(h["x1"]), int(h["y1"])])
            events.append(ev)

        # ---- rests
        for r in find_rests(nol, st, heads, x_start, page_index, si):
            events.append(r)

    events.sort(key=lambda e: (e.staff, e.x))
    # ---- ties between consecutive notes on the same step
    for a, b in zip(events, events[1:]):
        if a.kind == b.kind == "note" and a.staff == b.staff and a.step == b.step:
            if has_tie(clean, a, b, staves[a.staff]):
                a.tie_next = True

    # ---- pitch: staff step + accidentals carried through the measure
    for si, st in enumerate(staves):
        carry = {}
        bars = st.barlines
        for e in [e for e in events if e.staff == si]:
            m = int(np.searchsorted(bars, e.x))
            if e.measure != m:
                pass
            e.measure = m
        cur_m = None
        for e in [e for e in events if e.staff == si and e.kind == "note"]:
            if e.measure != cur_m:
                carry, cur_m = {}, e.measure
            acc = e.acc if e.acc is not None else carry.get(e.step, 0)
            if e.acc is not None:
                carry[e.step] = e.acc
            e.midi = step_to_midi(e.step, acc)

    # ---- printed labels: confirm / correct pitches, attach lyrics
    attach_labels(events, staves, sarg, west, result["warnings"])
    attach_lyrics(events, staves, lyric_tokens)

    result["staves"] = [asdict(s) for s in staves]
    result["events"] = events
    return result


def music_start(nol, st, time_sig=True):
    """x where notes can begin: after the clef and any time signature."""
    sp = st.sp
    y0, y1 = int(st.top - 3 * sp), int(st.bottom + 3 * sp)
    x_end = int(st.x0 + 12 * sp)
    region = nol[y0:y1, st.x0:x_end]
    lab, n = ndi.label(region, structure=np.ones((3, 3)))
    objs = [o for o in ndi.find_objects(lab) if o is not None]
    clef_x1 = st.x0 + 2.6 * sp
    for o in objs:
        hh = o[0].stop - o[0].start
        if hh > 4.5 * sp and o[1].start < 2 * sp:
            clef_x1 = max(clef_x1, o[1].stop + st.x0)
    start = clef_x1
    if not time_sig:
        return int(start + 0.3 * sp)
    # time signature: glyphs right after the clef that together fill the staff height
    parts = []
    for o in objs:
        hh = o[0].stop - o[0].start
        xa = o[1].start + st.x0
        top = o[0].start + y0
        bot = o[0].stop - 1 + y0
        if xa < clef_x1 - 2 or xa > clef_x1 + 2.5 * sp:
            continue
        if hh >= 0.5 * sp and top >= st.top - 0.5 * sp and bot <= st.bottom + 0.5 * sp:
            parts.append((top, bot, xa, o[1].stop + st.x0))
    if parts:
        cover = np.zeros(int(st.bottom - st.top) + 1, bool)
        for top, bot, _, _ in parts:
            cover[max(0, int(top - st.top)):max(0, int(bot - st.top)) + 1] = True
        if cover.mean() > 0.7:
            start = max(start, max(p[3] for p in parts))
    return int(start + 0.3 * sp)


def ring_ok(img, h, sp, wmin=0.9, wmax=1.8):
    """Half-note head: the outline around the hole spans ~1.0-1.7 spaces on the centre row."""
    y = int(round(h["cy"]))
    xa = int(h["cx"] - 1.2 * sp)
    row = img[y, max(0, xa):int(h["cx"] + 1.2 * sp) + 1]
    c = int(h["cx"]) - max(0, xa)
    if not (0 <= c < len(row)) or row[c]:
        return False
    l = c
    while l > 0 and not row[l - 1]:
        l -= 1
    r = c
    while r < len(row) - 1 and not row[r + 1]:
        r += 1
    hole = r - l + 1
    if not (0.2 * sp <= hole <= 0.95 * sp) or l == 0 or r == len(row) - 1:
        return False
    ll = l
    while ll > 0 and row[ll - 1]:
        ll -= 1
    rr = r
    while rr < len(row) - 1 and row[rr + 1]:
        rr += 1
    width = (rr + 1) - ll
    return wmin * sp <= width <= wmax * sp


def text_glyph_mask(black, tokens, staves):
    """Pixels of printed label text: glyphs fully inside an OCR box in a staff's label zone."""
    mask = np.zeros_like(black)
    lab, _ = ndi.label(black, structure=np.ones((3, 3)))
    objs = ndi.find_objects(lab)
    H, W = black.shape
    for o in tokens:
        if not re.search(r"[A-Za-z0-9]", o["t"]):
            continue
        ty0, ty1 = o["y"], o["y"] + o["h"]
        if any(ty1 > s.top - 0.6 * s.sp and ty0 < s.bottom + 0.6 * s.sp for s in staves):
            continue
        above = [s for s in staves if s.bottom < ty0]
        if above and ty0 - max(s.bottom for s in above) < 1.8 * above[-1].sp:
            continue  # just under a staff: could be low notes, ledger lines, dots
        x0, x1 = max(0, int(o["x"]) - 3), min(W, int(o["x"] + o["w"]) + 4)
        y0, y1 = max(0, int(ty0) - 3), min(H, int(ty1) + 4)
        for v in np.unique(lab[y0:y1, x0:x1]):
            if not v:
                continue
            sl = objs[v - 1]
            if sl[0].start >= y0 and sl[0].stop <= y1 and sl[1].start >= x0 and sl[1].stop <= x1:
                mask[sl] |= lab[sl] == v
    return mask


def probe_hollow(nol, hx, hy, sp):
    """Is there a half-note style head (ring) centred near (hx, hy)?"""
    H, W = nol.shape
    x0, x1 = int(hx - 0.75 * sp), int(hx + 0.75 * sp)
    y0, y1 = int(hy - 0.6 * sp), int(hy + 0.6 * sp)
    if x0 < 0 or y0 < 0 or x1 >= W or y1 >= H:
        return None
    win = nol[y0:y1 + 1, x0:x1 + 1]
    if win.mean() < 0.22:
        return None
    filled = ndi.binary_fill_holes(win)
    hole = filled & ~win
    lab, n = ndi.label(hole)
    if n == 0:
        return None
    sizes = ndi.sum(hole, lab, range(1, n + 1))
    k = int(np.argmax(sizes)) + 1
    if sizes[k - 1] < 0.05 * sp * sp:
        return None
    ys, xs = np.where(lab == k)
    # a staff or ledger line through the head splits the hole: merge the halves
    for j in range(1, n + 1):
        if j == k or sizes[j - 1] < 0.03 * sp * sp:
            continue
        yj, xj = np.where(lab == j)
        if abs(xj.mean() - xs.mean()) < 0.35 * sp and abs(yj.mean() - ys.mean()) < 0.7 * sp:
            ys, xs = np.concatenate([ys, yj]), np.concatenate([xs, xj])
    cy, cx = ys.mean() + y0, xs.mean() + x0
    return dict(cx=cx, cy=cy, x0=int(cx - 0.6 * sp), x1=int(cx + 0.6 * sp),
                y0=int(cy - 0.5 * sp), y1=int(cy + 0.5 * sp), w=1.2 * sp, h=sp, area=0,
                hollow=True, small=False)


def find_whole_notes(clean, nol, st, heads, x_start):
    """Stemless hollow heads."""
    sp = st.sp
    out = []
    y0 = max(0, int(st.top - 2.6 * sp))
    y1 = int(st.bottom + 2.6 * sp)
    region = clean[y0:y1, x_start:st.x1 + 1]
    filled = ndi.binary_fill_holes(region)
    hole = filled & ~region
    lab, n = ndi.label(hole)
    for i, sl in enumerate(ndi.find_objects(lab)):
        if sl is None:
            continue
        hh = sl[0].stop - sl[0].start
        ww = sl[1].stop - sl[1].start
        if not (0.25 * sp <= hh <= 0.8 * sp and 0.3 * sp <= ww <= 1.0 * sp):
            continue
        cy = (sl[0].start + sl[0].stop - 1) / 2 + y0
        cx = (sl[1].start + sl[1].stop - 1) / 2 + x_start
        if any(abs(h["cx"] - cx) < 1.0 * sp and abs(h["cy"] - cy) < 1.0 * sp for h in heads):
            continue
        # a whole note is a squat isolated ring: mass around the hole, nothing above/below it
        box = nol[int(cy - 0.55 * sp):int(cy + 0.55 * sp) + 1, int(cx - 0.95 * sp):int(cx + 0.95 * sp) + 1]
        above = nol[int(cy - 1.4 * sp):int(cy - 0.7 * sp), int(cx - 0.7 * sp):int(cx + 0.7 * sp) + 1]
        below = nol[int(cy + 0.7 * sp):int(cy + 1.4 * sp), int(cx - 0.7 * sp):int(cx + 0.7 * sp) + 1]
        if box.size == 0 or box.mean() < 0.3:
            continue
        busy = False
        for w in (above, below):
            if w.size and w.mean() > 0.06 and (w.any(1).mean() > 0.45 or (w.mean(0) > 0.6).any()):
                busy = True  # letters, sharps, naturals; a thin tie arc is allowed
        if busy:
            continue
        if not ring_ok(clean, dict(cx=cx, cy=cy), sp, 1.3, 2.3):
            continue
        # centre from the outline (the hole may be split by a staff line)
        cols = nol[max(0, int(cy - 1.0 * sp)):int(cy + 1.0 * sp) + 1, int(cx - 0.8 * sp):int(cx + 0.8 * sp) + 1]
        rows = cols.sum(1) >= 2
        r_hole = int(cy) - max(0, int(cy - 1.0 * sp))
        if 0 <= r_hole < len(rows):
            top = r_hole
            while top > 0 and (rows[top - 1] or top > r_hole - 2):
                top -= 1
            bot = r_hole
            while bot < len(rows) - 1 and (rows[bot + 1] or bot < r_hole + 2):
                bot += 1
            if 0.7 * sp <= bot - top + 1 <= 1.4 * sp:
                cy = max(0, int(cy - 1.0 * sp)) + (top + bot) / 2
        # a ledger line through the hole splits it in two: merge with a neighbour
        dup = next((o for o in out if abs(o["cx"] - cx) < 0.6 * sp and abs(o["cy"] - cy) < 0.9 * sp), None)
        if dup:
            dup["cy"] = (dup["cy"] + cy) / 2
            dup["y0"], dup["y1"] = int(dup["cy"] - 0.5 * sp), int(dup["cy"] + 0.5 * sp)
            continue
        out.append(dict(cx=cx, cy=cy, x0=int(cx - 0.8 * sp), x1=int(cx + 0.8 * sp),
                        y0=int(cy - 0.5 * sp), y1=int(cy + 0.5 * sp), w=1.6 * sp, h=sp,
                        area=0, hollow=True, small=False, stem=0, stem_seg=None))
    return out


def count_beams(nol, seg, direction, sp):
    """Count beam/flag strokes at the free end of a stem.

    Beams and flags leave the stem sideways near its tip, so a stroke only counts
    if it starts within ~1.5 spaces of the tip and is joined to the stem.
    """
    H, W = nol.shape
    tip = seg["y0"] if direction == 1 else seg["y1"]
    if direction == 1:
        ya, yb = int(tip - 0.25 * sp), int(tip + 1.5 * sp)
    else:
        ya, yb = int(tip - 1.5 * sp), int(tip + 0.25 * sp)
    ya, yb = max(0, ya), min(H, yb)
    best = 0
    for side in (1, -1):
        base = seg["xr"] if side == 1 else seg["xl"]
        for off in (0.45, 0.7):
            x = int(round(base + side * off * sp))
            if not (0 <= x < W):
                continue
            col = nol[ya:yb, max(0, x - 1):x + 2].any(1)
            s, e = runs(col)
            n = 0
            for a, b in zip(s, e):
                if (b - a) < 0.22 * sp:
                    continue
                xa, xb = (base, x) if side == 1 else (x, base)
                band = nol[ya + a:ya + b, xa:xb + 1]
                # joined: some row of the stroke is dark all the way to the stem (1px slack)
                if band.size and (band.sum(1) >= band.shape[1] - 1).any():
                    n += 1
            best = max(best, min(n, 3))
    return best


def count_dots(nol, h, st):
    sp = st.sp
    H, W = nol.shape
    x0 = int(h["x1"] + 0.15 * sp)
    x1 = int(h["x1"] + 1.4 * sp)
    r = ((st.bottom - h["cy"]) / (sp / 2)) % 2
    on_line = min(r, 2 - r) < 0.5
    yc = h["cy"] - (0.5 * sp if on_line else 0)
    y0, y1 = int(yc - 0.55 * sp), int(yc + 0.55 * sp)
    if x1 >= W or y0 < 0:
        return 0
    win = nol[y0:y1 + 1, x0:x1 + 1]
    lab, n = ndi.label(win)
    dots = 0
    for i, sl in enumerate(ndi.find_objects(lab)):
        hh = sl[0].stop - sl[0].start
        ww = sl[1].stop - sl[1].start
        if 0.22 * sp <= hh <= 0.6 * sp and 0.22 * sp <= ww <= 0.6 * sp:
            # must be isolated (not part of something bigger outside the window)
            gy0, gx0 = sl[0].start + y0, sl[1].start + x0
            ring = nol[max(0, gy0 - 2):gy0 + hh + 2, max(0, gx0 - 2):gx0 + ww + 2]
            inner = nol[gy0:gy0 + hh, gx0:gx0 + ww]
            if ring.sum() - inner.sum() <= 2:
                dots += 1
    return min(dots, 2)


def detect_accidental(nol, h, st):
    """Return -1 (flat), +1 (sharp), 0 (natural) or None when no accidental."""
    sp = st.sp
    x1 = int(h["x0"] - 0.08 * sp)
    x0 = int(h["x0"] - 1.9 * sp)
    y0 = int(h["cy"] - 2.2 * sp)
    y1 = int(h["cy"] + 1.8 * sp)
    if x0 < 0 or y0 < 0:
        return None
    x1 = int(h["x0"] + 0.25 * sp)
    win = nol[y0:y1 + 1, x0:x1 + 1].copy()
    # erase our own head (and its stem column) so a touching accidental stays separate
    hy0, hy1 = int(h["y0"] - y0 - 1), int(h["y1"] - y0 + 2)
    win[max(0, hy0):hy1, max(0, int(h["x0"] - x0 - 1)):] = False
    seg = h.get("stem_seg")
    if seg:
        win[max(0, seg["y0"] - y0):max(0, seg["y1"] - y0 + 1),
            max(0, seg["xl"] - x0 - 1):max(0, seg["xr"] - x0 + 2)] = False
    # bridge 1-2 px anti-aliasing gaps (a flat's stem and bowl can binarize apart)
    win = ndi.binary_closing(win, structure=np.ones((1, 4), bool)) | win
    lab, n = ndi.label(win, structure=np.ones((3, 3)))
    best = None
    for i, sl in enumerate(ndi.find_objects(lab)):
        hh = sl[0].stop - sl[0].start
        ww = sl[1].stop - sl[1].start
        if hh < 1.6 * sp or ww < 0.4 * sp or ww > 1.5 * sp:
            continue
        top = sl[0].start + y0
        bot = sl[0].stop - 1 + y0
        if not (top < h["cy"] < bot):
            continue
        # a stem/beam column reaching far beyond the accidental zone is not an accidental
        if top < h["cy"] - 2.15 * sp or bot > h["cy"] + 1.75 * sp:
            continue
        comp = lab[sl] == i + 1
        rows_w = comp.sum(1)
        third = max(1, hh // 3)
        top_w = rows_w[:third].mean()
        bot_w = rows_w[-third:].mean()
        cols = comp.sum(0)
        tall_cols = (cols > 0.6 * hh).sum()
        above = h["cy"] - top
        below = bot - h["cy"]
        if above > 1.3 * below and bot_w > 1.6 * top_w:
            kind = -1                                  # flat: tall stem, bowl at bottom
        elif ww >= 0.75 * sp and tall_cols >= 2 and abs(above - below) < 0.8 * sp:
            # sharp has two verticals spanning most of the height
            kind = 1 if comp[hh // 2 - 1:hh // 2 + 2].sum(1).mean() > 0.5 * ww else 0
        else:
            kind = 0
        dist = h["x0"] - (sl[1].stop + x0)
        if best is None or dist < best[0]:
            best = (dist, kind)
    return None if best is None else best[1]


def find_rests(nol, st, heads, x_start, page_index, si):
    sp = st.sp
    y0 = int(st.top - 1.0 * sp)
    y1 = int(st.bottom + 1.0 * sp)
    region = nol[y0:y1 + 1, x_start:st.x1 + 1]
    lab, n = ndi.label(region, structure=np.ones((3, 3)))
    out = []
    for i, sl in enumerate(ndi.find_objects(lab)):
        hh = sl[0].stop - sl[0].start
        ww = sl[1].stop - sl[1].start
        cx = (sl[1].start + sl[1].stop - 1) / 2 + x_start
        top = sl[0].start + y0
        bot = sl[0].stop - 1 + y0
        if any(h["x0"] - 0.5 * sp <= cx <= h["x1"] + 0.5 * sp for h in heads):
            continue
        if any(abs(cx - b) < 0.6 * sp for b in st.barlines):
            continue
        x1c = sl[1].stop - 1 + x_start
        if any(h["x0"] - 2.2 * sp <= x1c <= h["x0"] + 0.2 * sp and top - 0.3 * sp <= h["cy"] <= bot + 0.3 * sp
               for h in heads):
            continue  # accidental in front of a note
        comp = lab[sl] == i + 1
        fill = comp.mean()
        dur = None
        # touching the region border -> part of something bigger (stems, beams)
        if sl[0].start == 0 or sl[0].stop >= region.shape[0]:
            continue
        if 0.8 * sp <= ww <= 1.9 * sp and 0.35 * sp <= hh <= 0.85 * sp and fill > 0.7:
            mid = st.lines[2]
            second = st.lines[1]
            if abs(bot - mid) < 0.3 * sp:
                dur = 2.0
            elif abs(top - second) < 0.3 * sp:
                dur = 4.0
            else:
                dur = 2.0
        elif 2.2 * sp <= hh <= 3.4 * sp and 0.6 * sp <= ww <= 1.3 * sp and top > st.top - 0.6 * sp:
            dur = 1.0
        elif 1.3 * sp <= hh <= 2.2 * sp and 0.6 * sp <= ww <= 1.3 * sp:
            dur = 0.5
        if dur is None:
            continue
        out.append(Event(kind="rest", dur=dur, x=cx, y=(top + bot) / 2, staff=si, page=page_index,
                         bbox=[int(sl[1].start + x_start), int(top), int(sl[1].stop - 1 + x_start), int(bot)]))
    return out


def has_tie(nol, a: Event, b: Event, st: Staff) -> bool:
    sp = st.sp
    xa, xb = a.bbox[2] + 2, b.bbox[0] - 2
    if xb - xa < 0.5 * sp:
        return False
    cy = a.y
    # ties curve away from the stem: below for up-stems, above for down-stems
    if a.stem >= 0:
        ya, yb = int(cy + 0.2 * sp), int(cy + 2.2 * sp)
    else:
        ya, yb = int(cy - 2.2 * sp), int(cy - 0.2 * sp)
    win = nol[ya:yb, xa:xb]
    if win.size == 0:
        return False
    # Outside the staff-line bands any ink counts. Inside a band, a column counts
    # only when the line is clearly thicker than usual there (line + arc overlap).
    outside = win.copy()
    thick_in_band = np.zeros(win.shape[1], bool)
    for ly in st.lines:
        r0, r1 = int(round(ly)) - 1 - ya, int(round(ly)) + 1 - ya
        if r1 < 0 or r0 >= win.shape[0]:
            continue
        r0c, r1c = max(0, r0), min(win.shape[0] - 1, r1)
        outside[r0c:r1c + 1] = False
        cnt = win[max(0, r0c - 2):r1c + 3].sum(0)
        typical = np.median(cnt) if cnt.size else 0
        thick_in_band |= cnt >= typical + 1
    cols = outside.any(0) | thick_in_band
    s_, e_ = runs(~cols)
    max_gap = (e_ - s_).max() if len(s_) else 0
    return cols.mean() > 0.7 and max_gap < 1.0 * sp


def _match_labels(notes, staves, toks):
    """Greedy one-to-one match of label tokens to notes (labels are left-aligned)."""
    pairs = []
    for ni, e in enumerate(notes):
        st = staves[e.staff]
        sp = st.sp
        hx0 = e.bbox[0]
        for ti, o in enumerate(toks):
            cy = o["y"] + o["h"] / 2
            if not (st.top - 9 * sp < cy < st.top + 0.5 * sp):
                continue
            # label must not sit above the staff above
            if any(s2.top < st.top and cy < s2.bottom + 0.5 * s2.sp for s2 in staves if s2 is not st):
                continue
            dx = hx0 - o["x"]
            d = dx if dx >= 0 else -2 * dx
            lim = 2.0 * sp if (e.acc is not None) else 0.9 * sp
            if d < lim:
                pairs.append((d, ni, ti))
    pairs.sort()
    used_n, used_t, out = set(), set(), {}
    for d, ni, ti in pairs:
        if ni in used_n or ti in used_t:
            continue
        used_n.add(ni)
        used_t.add(ti)
        out[ni] = toks[ti]["t"]
    return out


def attach_labels(events, staves, sarg, west, warnings):
    notes = [e for e in events if e.kind == "note"]
    sl = _match_labels(notes, staves, sarg)
    wl = _match_labels(notes, staves, west)
    for ni, e in enumerate(notes):
        e.label = sl.get(ni, "")
        e.wlabel = wl.get(ni, "")
        lab_midi = sargam_label_to_midi(e.label) if e.label else None
        w_pc = west_to_pc(e.wlabel) if e.wlabel else None
        if lab_midi is not None and e.midi is not None and lab_midi != e.midi:
            agree_w = w_pc is not None and w_pc == lab_midi % 12
            if abs(lab_midi - e.midi) <= 2 and (agree_w or abs(lab_midi - e.midi) == 1):
                e.notes.append(f"pitch {e.midi}->{lab_midi} from label {e.label}")
                e.midi = lab_midi
            elif (lab_midi - e.midi) % 12:
                warnings.append(f"page {e.page+1} staff {e.staff+1}: image says {e.midi}, label {e.label} says {lab_midi}")
                e.conf = 0.5
        elif lab_midi is None and w_pc is not None and e.midi is not None and w_pc != e.midi % 12:
            delta = ((w_pc - e.midi % 12 + 6) % 12) - 6
            if abs(delta) == 1:
                e.notes.append(f"accidental from label {e.wlabel}")
                e.midi += delta


def attach_lyrics(events, staves, lyric_tokens):
    for o in lyric_tokens:
        cx = o["x"] + o["w"] / 2
        cy = o["y"] + o["h"] / 2
        # lyrics sit below their staff
        cands = [i for i, s in enumerate(staves) if s.bottom < cy < s.bottom + 7 * s.sp]
        if not cands:
            continue
        si = cands[0]
        notes = [e for e in events if e.kind == "note" and e.staff == si and not e.grace]
        if not notes:
            continue
        n = min(notes, key=lambda e: abs(e.x - (o["x"] + min(o["w"], 1.2 * staves[si].sp) / 2 + 0.3 * staves[si].sp)))
        if abs(n.x - cx) < 4 * staves[si].sp:
            n.lyric = (n.lyric + " " + o["t"]).strip() if n.lyric else o["t"]
