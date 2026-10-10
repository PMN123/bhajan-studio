"""Turn a Sai Rhythms sheet-music PDF into a playable score (JSON-friendly)."""
from __future__ import annotations

import collections
import json
import os
import re

import pymupdf as fitz
import numpy as np
from PIL import Image

from . import omr

DPI = 250
SCHEMA = 4  # bump to invalidate cached scores when the reader changes


def render_pages(pdf_path: str, out_dir: str) -> list[str]:
    doc = fitz.open(pdf_path)
    paths = []
    for i, page in enumerate(doc):
        p = os.path.join(out_dir, f"page{i}.png")
        if not os.path.exists(p):
            pix = page.get_pixmap(dpi=DPI, colorspace=fitz.csRGB, alpha=False)
            pix.save(p)
        paths.append(p)
    return paths


def _tempo_retry(png: str, rgb: np.ndarray) -> int | None:
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
    for o in omr.run_ocr(tmp):
        m = re.search(r"(\d{2,3})", o["t"])
        if m and 40 <= int(m.group(1)) <= 240:
            return int(m.group(1))
    return None


def read_sheet(pdf_path: str, work_dir: str) -> dict:
    os.makedirs(work_dir, exist_ok=True)
    cache = os.path.join(work_dir, "score.json")
    if os.path.exists(cache):
        with open(cache) as f:
            score = json.load(f)
        if score.get("schema") == SCHEMA:
            return score

    pages = render_pages(pdf_path, work_dir)
    all_events, page_info, warnings = [], [], []
    tempo = None
    measure_base = 0
    staff_base = 0
    for pi, png in enumerate(pages):
        rgb = np.asarray(Image.open(png).convert("RGB"))
        res = omr.read_page(rgb, pi, omr.run_ocr(png))
        page_info.append({"w": rgb.shape[1], "h": rgb.shape[0], "staves": len(res["staves"])})
        if not res["staves"]:
            continue
        if pi == 0:
            tempo = res["tempo"]
            if tempo is None or not (40 <= tempo <= 240):
                tempo = _tempo_retry(png, rgb)
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
    with open(cache, "w") as f:
        json.dump(score, f)
    return score
