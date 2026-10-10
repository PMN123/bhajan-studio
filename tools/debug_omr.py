"""Debug: run OMR on a PNG page and draw detections. usage: debug_omr.py page.png out.png"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from bhajan import omr

src, dst = sys.argv[1], sys.argv[2]
rgb = np.asarray(Image.open(src).convert("RGB"))
toks = omr.run_ocr(src)
res = omr.read_page(rgb, 0, toks)
im = Image.fromarray(rgb).convert("RGB"); dr = ImageDraw.Draw(im)
try: font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 15)
except Exception: font = None
for s in res["staves"]:
    for b in s["barlines"]:
        dr.line([(b, s["lines"][0] - 10), (b, s["lines"][4] + 10)], fill=(0, 200, 0), width=3)
for e in res["events"]:
    x0, y0, x1, y1 = e.bbox
    col = (255, 0, 0) if e.kind == "note" else (0, 150, 255)
    if e.conf < 1: col = (255, 0, 255)
    dr.rectangle([x0 - 2, y0 - 2, x1 + 2, y1 + 2], outline=col, width=2)
    txt = (omr.sargam_name(e.midi) if e.kind == "note" else "R") + f" {e.dur:g}" + ("~" if e.tie_next else "")
    dr.text((x0 - 4, y1 + 22), txt, fill=col, font=font)
im.save(dst)
print("tempo", res["tempo"], "warnings", res["warnings"][:8])
cur = None; tot = 0; line = []
for e in res["events"]:
    key = (e.staff, e.measure)
    if key != cur:
        if cur: print(cur, f"sum={tot:g}", " ".join(line))
        cur, tot, line = key, 0, []
    tot += e.dur
    line.append((omr.sargam_name(e.midi) if e.kind == "note" else "rest") + f":{e.dur:g}" + ("~" if e.tie_next else "") + (f"[{e.label}/{e.wlabel}]" if e.label or e.wlabel else "") + (f"'{e.lyric}'" if e.lyric else ""))
if cur: print(cur, f"sum={tot:g}", " ".join(line))
