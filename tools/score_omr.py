"""Quick quality metrics for OMR over page PNGs: bar-sum consistency and label agreement."""
import sys, os, collections
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from PIL import Image
from bhajan import omr
for src in sys.argv[1:]:
    rgb = np.asarray(Image.open(src).convert("RGB"))
    res = omr.read_page(rgb, 0 if src.endswith("_p0.png") else 1, omr.run_ocr(src))
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
