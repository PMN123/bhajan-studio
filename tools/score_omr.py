"""Quick quality metrics for OMR over page PNGs: bar-sum consistency and label agreement.

usage: uv run python tools/score_omr.py [--ocr auto|vision|tesseract|none] page.png ...
(--ocr defaults to $BHAJAN_OCR, else auto; the engine and OCR time go to stderr)
"""
import argparse, collections, os, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from PIL import Image
from bhajan import omr

ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument("--ocr", choices=omr.OCR_ENGINES, default=None,
                help="OCR engine (default: $BHAJAN_OCR or auto)")
ap.add_argument("pages", nargs="+")
args = ap.parse_args()
print(f"OCR engine: {omr.ocr_engine(args.ocr)}", file=sys.stderr)
ocr_secs = []
for src in args.pages:
    rgb = np.asarray(Image.open(src).convert("RGB"))
    t = time.perf_counter()
    toks = omr.run_ocr(src, args.ocr)
    ocr_secs.append(time.perf_counter() - t)
    res = omr.read_page(rgb, 0 if src.endswith("_p0.png") else 1, toks)
    ev = res["events"]; notes = [e for e in ev if e.kind == "note"]
    sums = collections.defaultdict(float)
    for e in ev: sums[(e.staff, e.measure)] += e.dur
    mode = collections.Counter(sums.values()).most_common(1)[0][0] if sums else 0
    bad = {k: v for k, v in sums.items() if v != mode}
    lab = [e for e in notes if e.label]
    agree = sum(1 for e in lab if omr.sargam_label_to_midi(e.label) == e.midi)
    print(f"{os.path.basename(src)[:38]:38s} notes={len(notes):3d} rests={len(ev)-len(notes):2d} tempo={res['tempo']} bar={mode:g} "
          f"badbars={len(bad)}/{len(sums)} {dict((f'{k[0]}.{k[1]}', v) for k, v in bad.items())} labels={len(lab)} agree={agree}")
    for e in lab:
        if omr.sargam_label_to_midi(e.label) != e.midi:
            print("    mismatch staff", e.staff, "m", e.measure, "x", int(e.x), "img", omr.sargam_name(e.midi), "label", e.label)
if ocr_secs:
    print(f"OCR time: {sum(ocr_secs):.1f}s for {len(ocr_secs)} pages "
          f"(mean {sum(ocr_secs) / len(ocr_secs):.3f}s, max {max(ocr_secs):.3f}s)", file=sys.stderr)
