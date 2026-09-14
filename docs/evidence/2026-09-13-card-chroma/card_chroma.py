"""THE DETECTOR EXPERIMENT: grey-level edges (shipped) vs a chroma edge, same gates.

The chroma variant hands find_reference an image whose three channels all equal the
pixel's chroma, C = sqrt(a*^2 + b*^2) from Lab, times a gain. find_reference converts
to grey, so it sees chroma, and every gate, tolerance and the consensus rule are the
shipped ones -- the only change is what the edges are computed on.
Chroma, not green: the card in photo 15 is RED. A green key would be fitted to one card.

Frames: exactly what production analyses -- EXIF-rotated, thumbnailed to 1280.
Truth:  card_boxes.json (12 Sep audit, colour-boxed and checked on a contact sheet) for
        the grey detector's misses; the grey detector's verified quads for its 5 hits.
Hit:    detection polygon IoU >= 0.5 with the truth box. A detection anywhere else is a
        FALSE POSITIVE, on a card photo or not.
"""
from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

REPO = Path(r"C:\Users\VetaM\Downloads\nutriai\nutriai")
os.chdir(REPO / "backend")
sys.path.insert(0, str(REPO / "backend"))
import logging  # noqa: E402

import structlog  # noqa: E402

structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
from app.services.ai.reference_cv import find_reference  # noqa: E402
from app.services.ai.vision import MAX_IMAGE_EDGE  # noqa: E402

HERE = Path(__file__).resolve().parent
AUDIT = Path(__file__).resolve().parent / "card_boxes_2026-09-12.json"   # 12 Sep audit, copied from its scratchpad
BOXES = json.loads(AUDIT.read_text(encoding="utf-8"))
GAINS = (2.0, 3.0, 4.0)

CARD = ["10-tacos-paper-card", "11-kebab-paper-card", "14-plate-mole-chicken-card", "15-rajas-rice-card"] + \
       [f"{n}-" for n in range(17, 33)] + ["34-", "36-", "40-", "41-", "42-", "43-", "44-", "45-"]
NOCARD = [f"0{n}-" for n in range(1, 10)] + ["12-", "13-", "16-", "33-", "35-", "IMG_3938"]
EXTRA_NEG = ["08b-", "08c-"]


def find_file(prefix):
    roots = [REPO / "photos", REPO / "photos" / "legacy", Path.home() / "Downloads"]
    for r in roots:
        for p in sorted(r.glob(prefix + "*")):
            if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".heic") and p.is_file():
                if prefix.endswith("-") and not p.name.startswith(prefix):
                    continue
                return p
    return None


def frame(path):
    img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
    img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE), Image.LANCZOS)
    return img


def chroma_image(img, gain):
    lab = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2LAB).astype(np.float32)
    c = np.sqrt((lab[:, :, 1] - 128) ** 2 + (lab[:, :, 2] - 128) ** 2)
    c = np.clip(c * gain, 0, 255).astype(np.uint8)
    return Image.fromarray(np.dstack([c, c, c]))


def poly_iou(a, b, size):
    w, h = size
    ma, mb = np.zeros((h, w), np.uint8), np.zeros((h, w), np.uint8)
    cv2.fillPoly(ma, [np.round(a).astype(np.int32)], 1)
    cv2.fillPoly(mb, [np.round(b).astype(np.int32)], 1)
    inter, union = (ma & mb).sum(), (ma | mb).sum()
    return inter / union if union else 0.0


def truth_for(name, size, grey_ref):
    key = next((k for k in BOXES if k.startswith(name.split(".")[0])), None)
    box = BOXES.get(key) if key else None
    if box:
        fw, fh = box["frame"]
        s = size[0] / fw
        return np.array(box["pts"]) * s, "audit colour box"
    if grey_ref is not None:
        return np.array(grey_ref.corners), "grey detector quad (verified hit)"
    return None, "no truth box"


rows, sheet = [], []
for group, prefixes in (("card", CARD), ("nocard", NOCARD), ("extra-neg", EXTRA_NEG)):
    for pre in prefixes:
        path = find_file(pre)
        if path is None:
            rows.append(dict(group=group, name=pre, missing=True))
            print(f"MISSING FILE for {pre}")
            continue
        img = frame(path)
        grey = find_reference(img)
        colour = {g: find_reference(chroma_image(img, g)) for g in GAINS}
        truth, tsrc = (truth_for(path.name, img.size, grey) if group == "card" else (None, "-"))
        rec = dict(group=group, name=path.name, size=img.size, truth_src=tsrc)
        for label, ref in [("grey", grey)] + [(f"chroma x{g:g}", colour[g]) for g in GAINS]:
            if ref is None:
                rec[label] = ("none", None, None)
                continue
            q = np.array(ref.corners)
            if group == "card" and truth is not None:
                iou = poly_iou(q, truth, img.size)
                tl = max(np.linalg.norm(truth[0] - truth[1]), np.linalg.norm(truth[1] - truth[2]))
                verdict = "HIT" if iou >= 0.5 else "FALSE POSITIVE (elsewhere)"
                rec[label] = (verdict, round(iou, 3), round(100 * (ref.long_px / tl - 1), 1))
            elif group == "card":
                rec[label] = ("detected, no truth box", None, None)
            else:
                rec[label] = ("FALSE POSITIVE", None, None)
            rec[label] = rec[label] + (ref.kind, ref.consensus, round(ref.frame_width_mm, 1))
        rows.append(rec)
        # contact tile: grey quad blue, chroma x3 quad magenta, truth green
        t = np.asarray(img).copy()
        if truth is not None:
            cv2.polylines(t, [np.round(truth).astype(np.int32)], True, (0, 255, 0), 3)
        if grey is not None:
            cv2.polylines(t, [np.round(np.array(grey.corners)).astype(np.int32)], True, (0, 90, 255), 4)
        for g, col in ((2.0, (255, 200, 0)), (3.0, (255, 0, 255)), (4.0, (255, 0, 0))):
            if colour[g] is not None:
                cv2.polylines(t, [np.round(np.array(colour[g].corners)).astype(np.int32)], True, col, 2)
        s = 300 / max(t.shape[:2])
        t = cv2.resize(t, (int(t.shape[1] * s), int(t.shape[0] * s)))
        t = cv2.copyMakeBorder(t, 18, 300 - t.shape[0], 0, 300 - t.shape[1], cv2.BORDER_CONSTANT, value=(255, 255, 255))
        cv2.putText(t, path.name[:30], (2, 13), cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 0, 0), 1)
        sheet.append(t)

labels = ["grey"] + [f"chroma x{g:g}" for g in GAINS]
print(f"frames analysed at MAX_IMAGE_EDGE={MAX_IMAGE_EDGE}; hit = IoU>=0.5 with the truth box\n")
print(f"{'photo':34s} {'truth':28s} " + " | ".join(f"{l:>34s}" for l in labels))
for r in rows:
    if r.get("missing"):
        print(f"{r['name']:34s} MISSING"); continue
    cells = []
    for l in labels:
        v = r[l]
        if v[0] == "none":
            cells.append(f"{'-':>34s}")
        else:
            extra = f" iou {v[1]} len {v[2]:+}%" if v[1] is not None else ""
            cells.append(f"{(v[0][:14] + extra + ' c' + str(v[4])):>34s}")
    print(f"{r['name'][:34]:34s} {r['truth_src'][:28]:28s} " + " | ".join(cells))

print("\nSUMMARY")
for l in labels:
    card = [r for r in rows if r["group"] == "card" and not r.get("missing")]
    hits = sum(r[l][0] == "HIT" for r in card)
    notruth = sum(r[l][0] == "detected, no truth box" for r in card)
    fp_card = sum(r[l][0].startswith("FALSE") for r in card)
    neg = [r for r in rows if r["group"] == "nocard" and not r.get("missing")]
    fp_neg = sum(r[l][0] == "FALSE POSITIVE" for r in neg)
    extra = [r for r in rows if r["group"] == "extra-neg" and not r.get("missing")]
    fp_extra = sum(r[l][0] == "FALSE POSITIVE" for r in extra)
    lens = [r[l][2] for r in card if r[l][0] == "HIT"]
    print(f"  {l:12s} recall {hits}/{len(card)}" + (f" (+{notruth} detected with no truth box)" if notruth else "") +
          f"   false positives: on card photos {fp_card}, on the {len(neg)} no-card photos {fp_neg}, on 08b/08c {fp_extra}"
          + (f"   hit long-side vs truth: median {np.median(lens):+.1f}%, range {min(lens):+.1f} to {max(lens):+.1f}" if lens else ""))
union = sum(any(r[l][0] == "HIT" for l in labels[:1] + ["chroma x3"]) for r in rows if r["group"] == "card" and not r.get("missing"))
print(f"  grey OR chroma x3 recall: {union}")

cols = 8
while len(sheet) % cols:
    sheet.append(np.full_like(sheet[0], 255))
grid = np.vstack([np.hstack(sheet[i:i + cols]) for i in range(0, len(sheet), cols)])
cv2.imwrite(str(HERE / "card_colour_sheet.png"), cv2.cvtColor(grid, cv2.COLOR_RGB2BGR))
json.dump(rows, open(HERE / "card_colour.json", "w"), indent=1, default=str)
print(f"\nsheet (green truth, blue grey, yellow x2, magenta x3, red x4): {HERE / 'card_colour_sheet.png'}")
