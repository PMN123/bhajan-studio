"""Turn a Sai Rhythms sheet-music PDF into a playable score (JSON-friendly)."""
from __future__ import annotations

import collections
import logging
import os
import re
import threading
import time

import pymupdf as fitz
import numpy as np
from PIL import Image

from . import omr, store

DPI = 250
SCHEMA = 5  # bump to invalidate cached scores when the reader changes
DEGRADED_RETRY = 600  # seconds before a sheet whose OCR failed is read again
_log = logging.getLogger(__name__)
_degraded: dict[str, tuple[float, dict]] = {}   # work_dir -> (time, score) for reads that are not cached on disk
_degraded_lock = threading.Lock()


def render_pages(pdf_path: str, out_dir: str) -> list[str]:
    doc = fitz.open(pdf_path)
    paths = []
    for i, page in enumerate(doc):
        p = os.path.join(out_dir, f"page{i}.png")
        if not os.path.exists(p):
            pix = page.get_pixmap(dpi=DPI, colorspace=fitz.csRGB, alpha=False)
            store.write_bytes(p, pix.tobytes("png"))
        paths.append(p)
    return paths


def _load_page(pdf_path: str, work_dir: str, png: str) -> np.ndarray:
    try:
        return np.asarray(Image.open(png).convert("RGB"))
    except OSError:  # damaged by an older version or a crash: render it again
        os.remove(png)
        render_pages(pdf_path, work_dir)
        return np.asarray(Image.open(png).convert("RGB"))


def _ocr(png: str, engine: str) -> tuple[list[dict], bool]:
    """(tokens, ok). ok is False when the OCR engine failed, as opposed to finding no text."""
    if engine == "none":
        return [], True
    try:
        return omr.run_ocr(png, engine=engine), True
    except Exception as e:
        _log.warning("OCR (%s) failed on %s (%s); reading the notes without the printed labels",
                     engine, png, e)
        return [], False


def _tempo_retry(png: str, rgb: np.ndarray, engine: str) -> int | None:
    """Re-OCR an enlarged crop around the red tempo mark."""
    _, _, red = omr.masks(rgb)
    if not red.any():
        return None
    ys, xs = np.where(red)
    y0, y1 = max(0, ys.min() - 15), ys.max() + 15
    x0, x1 = max(0, xs.min() - 15), min(rgb.shape[1], xs.max() + 200)
    crop = Image.fromarray(rgb[y0:y1, x0:x1]).resize(((x1 - x0) * 3, (y1 - y0) * 3), Image.LANCZOS)
    tmp = png[:-4] + "_tempo.png"
    crop.save(tmp)
    for o in _ocr(tmp, engine)[0]:
        m = re.search(r"(\d{2,3})", o["t"])
        if m and 40 <= int(m.group(1)) <= 240:
            return int(m.group(1))
    return None


def read_sheet(pdf_path: str, work_dir: str) -> dict:
    """Read a sheet PDF into a score, cached in work_dir/score.json.

    The cache is reused while SCHEMA and the available OCR engine are unchanged. A read in which the
    OCR engine failed is not written to disk (it lacks labels, lyrics and tempo); it is kept in memory
    and retried after DEGRADED_RETRY seconds, so a fixed or newly installed OCR engine gets used.
    """
    os.makedirs(work_dir, exist_ok=True)
    cache = os.path.join(work_dir, "score.json")
    engine = omr.ocr_engine()
    score = store.read_json(cache)
    if score is not None and score.get("schema") == SCHEMA and score.get("ocr") == engine:
        return score
    with _degraded_lock:
        hit = _degraded.get(work_dir)
    if hit and hit[1].get("ocr") == engine and time.time() - hit[0] < DEGRADED_RETRY:
        return hit[1]

    pages = render_pages(pdf_path, work_dir)
    all_events, page_info, warnings = [], [], []
    tempo = None
    measure_base = 0
    staff_base = 0
    ocr_ok = True
    for pi, png in enumerate(pages):
        rgb = _load_page(pdf_path, work_dir, png)
        tokens, ok = _ocr(png, engine)
        ocr_ok &= ok
        res = omr.read_page(rgb, pi, tokens)
        page_info.append({"w": rgb.shape[1], "h": rgb.shape[0], "staves": len(res["staves"])})
        if not res["staves"]:
            continue
        if pi == 0:
            tempo = res["tempo"]
            if tempo is None or not (40 <= tempo <= 240):
                tempo = _tempo_retry(png, rgb, engine)
        warnings += res["warnings"]
        for si, st in enumerate(res["staves"]):
            evs = [e for e in res["events"] if e.staff == si]
            bars = st["barlines"]
            # measures in this staff: one per barline, plus a trailing one if music follows the last bar
            n_meas = len(bars) + (1 if (not bars or bars[-1] < st["x1"] - 2 * st["sp"]) else 0)
            for e in evs:
                d = {
                    "kind": e.kind, "dur": e.dur, "midi": e.midi, "tie_next": e.tie_next,
                    "grace": e.grace, "lyric": e.lyric, "label": e.label, "wlabel": e.wlabel,
                    "page": pi, "staff": staff_base + si, "measure": measure_base + e.measure,
                    "bbox": e.bbox, "conf": e.conf,
                }
                if e.kind == "note":
                    d["sargam"] = omr.sargam_name(e.midi)
                all_events.append(d)
            measure_base += max(n_meas, 1)
        staff_base += len(res["staves"])

    # bar length: most common measure total (pickups and endings are allowed to differ)
    sums = collections.defaultdict(float)
    for e in all_events:
        sums[e["measure"]] += e["dur"]
    bar = collections.Counter(round(v, 3) for v in sums.values()).most_common(1)[0][0] if sums else 4.0
    # A bar is suspicious when its length differs from the usual bar AND from both
    # neighbours: a run of equal bars is a genuine meter change (some sheets switch
    # from 6/8 to 4/4 for the faster final section).
    ms = sorted(sums)
    inner_odd = []
    for k, m in enumerate(ms):
        if k in (0, len(ms) - 1) or abs(sums[m] - bar) < 1e-6:
            continue
        if abs(sums[ms[k - 1]] - sums[m]) > 1e-6 and abs(sums[ms[k + 1]] - sums[m]) > 1e-6:
            inner_odd.append(m)
    notes = [e for e in all_events if e["kind"] == "note"]
    labelled = [e for e in notes if e["label"]]
    agree = sum(1 for e in labelled if omr.sargam_label_to_midi(e["label"]) == e["midi"])
    score = {
        "schema": SCHEMA,
        "ocr": engine,
        "ocr_ok": ocr_ok,
        "tempo": tempo,
        "bar": bar,
        "pages": page_info,
        "events": all_events,
        "warnings": warnings,
        "stats": {
            "notes": len(notes),
            "measures": len(sums),
            "inconsistent_measures": sorted(inner_odd),
            "labelled": len(labelled),
            "label_agreement": (agree / len(labelled)) if labelled else None,
        },
    }
    if ocr_ok:
        store.write_json(cache, score)
        with _degraded_lock:
            _degraded.pop(work_dir, None)
    else:
        with _degraded_lock:
            _degraded[work_dir] = (time.time(), score)
    return score
