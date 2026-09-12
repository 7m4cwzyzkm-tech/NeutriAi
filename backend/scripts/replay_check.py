"""Three independent checks of the mask-selection rule. No network, no cost.

RUN THIS BEFORE ANY PAID BENCH. Twice now a change passed the whole unit
suite and was wrong on real photographs, because every fixture in that suite
draws a plate as a solid ellipse and SAM2 does not: it returns a plate with
the food punched out of it, and a table that is a ring around the plate.
Neither shape existed in any test, so neither was ever tested.

They are deliberately not three flavours of the same check:

  A. REPLAY -- the real photographs, the real plate found from pixels, and the
     real food shape taken from the colour rule, wrapped in the mask topology
     SAM2 actually returns (one mask per piece, a plate with the food punched
     out of it, a table). This is the geometry that walked past 456 green tests.

  B. REPRODUCTION -- the exact numbers the live bench logged when it broke:
     box 16% of frame, plate 39%, union 84.8%. If the fix is real, that same
     configuration must now come back as the food.

  C. FUZZ -- 400 random configurations, checking the invariants rather than
     any particular answer: no mask larger than the ceiling is ever selected,
     and a plate or table is never part of the union.
"""
import io, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import numpy as np, httpx
from PIL import Image, ImageOps
from app.services.ai import segment_hosted as S, food_seg

P = pathlib.Path(__file__).resolve().parents[2] / "photos"

# photos/ is gitignored, so a fresh checkout has none. Without this the module
# raises FileNotFoundError from top-level code and prints a raw traceback --
# and worse, a run over an empty set would report ALL THREE CHECKS PASS.
_NEEDED = ["17-carrots-plate.jpg", "18-zucchini-plate.jpg",
           "22-roast-beef-plate.jpg", "34-pizza-slice-plate.jpg"]
_MISSING = [n for n in _NEEDED if not (P / n).exists()]
if _MISSING:
    print(f"\n  Cannot run: {len(_MISSING)} weighed photo(s) missing from {P}")
    for n in _MISSING:
        print(f"    {n}")
    print("  This check is worthless without them -- it is not passing, it is "
          "not running.\n")
    sys.exit(2)

def load(name):
    im = ImageOps.exif_transpose(Image.open(P / name)).convert("RGB")
    im.thumbnail((1280, 1280), Image.LANCZOS)
    return np.asarray(im)

def png(m):
    buf = io.BytesIO()
    Image.fromarray(np.where(m, 255, 0).astype(np.uint8), "L").save(buf, "PNG")
    return buf.getvalue()

def provider(masks):
    urls = [f"https://cdn.example/m{i}.png" for i in range(len(masks))]
    def handler(request):
        u = str(request.url)
        if u.startswith("https://api.replicate.com"):
            return httpx.Response(201, json={"id": "p", "status": "succeeded",
                "output": {"individual_masks": urls},
                "urls": {"get": "https://api.replicate.com/v1/predictions/p"}})
        return httpx.Response(200, content=png(masks[int(u.rsplit("/m",1)[1].split(".")[0])]))
    return S.HostedSegmenter(dialect="replicate", api_key="k", version="v",
                             mode="auto", timeout_s=5,
                             transport=httpx.MockTransport(handler))

def box_around(seed, frac, shape):
    H, W = shape
    side = np.sqrt(frac)
    return {"x": max(0.0, seed[0]/W - side/2), "y": max(0.0, seed[1]/H - side/2),
            "w": side, "h": side}

# Quiet the segmenter's own logging. 400 fuzz trials emit 800 structured log
# lines and bury the four lines that carry the answer -- a check nobody can
# read is a check nobody runs.
try:
    import logging
    logging.getLogger().setLevel(logging.WARNING)
    import structlog
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(logging.WARNING))
except Exception:  # noqa: BLE001
    pass

fails = []
skipped: list = []
executed = 0

# ---------------------------------------------------------------- A. REPLAY
print("A. REPLAY -- real photos, real food shapes, SAM2's mask topology")
print(f"   {'photo':30s} {'food':>7s} {'chosen':>7s} {'plate?':>7s}  verdict")
for name in ("17-carrots-plate.jpg", "18-zucchini-plate.jpg",
             "22-roast-beef-plate.jpg", "34-pizza-slice-plate.jpg"):
    a = load(name); H, W = a.shape[:2]
    plate, _ = food_seg.plate_surface(a, None)
    ys, xs = np.nonzero(plate); seed = (int(xs.mean()), int(ys.mean()))
    pb = {"x": xs.min()/W, "y": ys.min()/H,
          "w": (xs.max()-xs.min())/W, "h": (ys.max()-ys.min())/H}
    food = food_seg.food_on_plate(a, pb)
    if food is None:
        # A SKIP IS NOT A PASS. Recorded, so a run where every photo skips
        # cannot print ALL THREE CHECKS PASS having verified nothing.
        print(f"   {name:30s} colour rule found nothing -- SKIPPED")
        skipped.append(name)
        continue
    food = food & plate
    n, lab, st, _ = __import__("cv2").connectedComponentsWithStats(
        food.astype(np.uint8), 8)
    pieces = [lab == i for i in range(1, n) if st[i, 4] > 200]
    if not pieces:
        pieces = [food]
    table = np.zeros((H, W), bool)
    table[int(H*0.02):int(H*0.98), int(W*0.02):int(W*0.98)] = True
    table &= ~plate
    holey = plate & ~food
    seg = provider(pieces + [holey, table])
    got = seg.segment_boxes(a, [seed], [box_around(seed, 0.16, (H, W))])
    chosen = got[0].mask if got and got[0] is not None else np.zeros((H, W), bool)
    leaked = float((chosen & holey).sum()) / max(int(holey.sum()), 1)
    ok = leaked < 0.02 and float(chosen.mean()) < 0.5
    fails.append(("A " + name, ok))
    print(f"   {name:30s} {food.mean():6.1%} {chosen.mean():6.1%} "
          f"{leaked:6.1%}  {'ok' if ok else 'FAIL'}")

# --------------------------------------------------------- B. REPRODUCTION
print("\nB. REPRODUCTION -- the exact configuration the live bench logged")
a = load("17-carrots-plate.jpg"); H, W = a.shape[:2]
plate, _ = food_seg.plate_surface(a, None)
ys, xs = np.nonzero(plate); seed = (int(xs.mean()), int(ys.mean()))
pb = {"x": xs.min()/W, "y": ys.min()/H, "w": (xs.max()-xs.min())/W,
      "h": (ys.max()-ys.min())/H}
food = food_seg.food_on_plate(a, pb) & plate
import cv2
n, lab, st, _ = cv2.connectedComponentsWithStats(food.astype(np.uint8), 8)
pieces = [lab == i for i in range(1, n) if st[i, 4] > 200]
table = np.zeros((H, W), bool); table[int(H*0.02):int(H*0.98), int(W*0.02):int(W*0.98)] = True
table &= ~plate
seg = provider(pieces + [plate & ~food, table])
got = seg.segment_boxes(a, [seed], [box_around(seed, 0.16, (H, W))])
area = float(got[0].mask.mean()) if got and got[0] is not None else float("nan")
print(f"   plate {plate.mean():.1%} of frame, box 16.0%, food {food.mean():.1%}")
print(f"   the bench logged 84.8%.  now: {area:.1%}")
# Compare against the food INSIDE THE BOX, not all the food on the plate. A
# 16% box centred on the plate does not contain all ten scattered carrots, and
# reporting only the ones it does contain is the correct answer to the question
# asked -- not a leak. The failure being tested for is the plate coming back.
bx = box_around(seed, 0.16, (H, W))
win = np.zeros((H, W), bool)
win[int(bx["y"]*H):int((bx["y"]+bx["h"])*H), int(bx["x"]*W):int((bx["x"]+bx["w"])*W)] = True
inbox = 0.0
for pc in pieces:
    yy2, xx2 = np.nonzero(pc)
    if win[int(yy2.mean()), int(xx2.mean())]:
        inbox += float(pc.mean())
print(f"   food whose centre is inside that box: {inbox:.1%}")
okB = area < 0.10 and abs(area - inbox) < 0.005
fails.append(("B reproduction", okB))
print(f"   {'ok' if okB else 'FAIL'}")

# ------------------------------------------------------------------ C. FUZZ
print("\nC. FUZZ -- 400 random layouts, checking invariants not answers")
rng = np.random.default_rng(11)
h, w = 240, 320
bad_ceiling = bad_plate = 0
for trial in range(400):
    yy, xx = np.mgrid[0:h, 0:w]
    npieces = int(rng.integers(1, 7))
    pieces = []
    bx, by = rng.uniform(0.2, 0.5), rng.uniform(0.2, 0.5)
    bw = bh = rng.uniform(0.15, 0.45)
    for _ in range(npieces):
        cx = rng.uniform(bx, bx + bw); cy = rng.uniform(by, by + bh)
        r = rng.uniform(0.02, 0.06)
        pieces.append((((xx - cx*w)/(r*w))**2 + ((yy - cy*h)/(r*h))**2) <= 1)
    big = (((xx - 0.5*w)/(0.45*w))**2 + ((yy - 0.5*h)/(0.45*h))**2) <= 1
    holey = big & ~np.logical_or.reduce(pieces)
    tbl = np.ones((h, w), bool) & ~big
    seg = provider(pieces + [holey, tbl])
    box = {"x": bx, "y": by, "w": bw, "h": bh}
    got = seg.segment_boxes(np.zeros((h, w, 3), np.uint8),
                            [(int((bx+bw/2)*w), int((by+bh/2)*h))], [box])
    if not got or got[0] is None:
        continue
    executed += 1
    m = got[0].mask
    box_px = (bw*w) * (bh*h)
    if float((m & holey).sum()) / max(int(holey.sum()), 1) > 0.05:
        bad_plate += 1
    if float((m & tbl).sum()) / max(int(tbl.sum()), 1) > 0.05:
        bad_plate += 1
    if int(m.sum()) > box_px * S.HostedSegmenter.MAX_MASK_OVER_BOX * npieces:
        bad_ceiling += 1
print(f"   plate or table leaked into the union: {bad_plate} of 400")
print(f"   union past the ceiling:               {bad_ceiling} of 400")
# Trials that produced no mask verified nothing. If almost none of them ran,
# the zeroes above are the absence of evidence, not evidence of absence.
print(f"   trials that actually produced a mask:  {executed} of 400")
okC = bad_plate == 0 and bad_ceiling == 0 and executed >= 200
fails.append(("C fuzz", okC))
print(f"   {'ok' if okC else 'FAIL'}")

print("\n" + "=" * 66)
bad = [n for n, ok in fails if not ok]
if skipped:
    print(f"SKIPPED (verified nothing): {', '.join(skipped)}")
    bad = bad + [f"skipped:{n}" for n in skipped]
if len(fails) < 5:
    bad = bad + [f"only {len(fails)} of 5 checks ran at all"]
print("ALL THREE CHECKS PASS" if not bad else "FAILED: " + ", ".join(bad))
print("=" * 66)
sys.exit(1 if bad else 0)
