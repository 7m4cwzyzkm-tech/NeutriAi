"""Is the segmenter configured, does the model take what we send it, and does
it come back with a mask? Answers all three before a bench run spends anything.

WHY THIS EXISTS

SEGMENTER_PROVIDER has been unset for weeks and every bench run has printed
"0 live" because of it. Switching it on is four .env lines and a model version
id, and getting any one of them wrong looks exactly the same from the outside:
the run completes, the numbers come out, and the footprint silently never
reaches the grams. That is the defect class this project keeps producing --
built, configured, and not connected -- so this asks the three questions
directly and prints the answers.

WHAT IT NEVER DOES

Print a key. Lengths and a four-character prefix only, enough to tell "the
wrong key" from "no key" and nothing more.
"""
from __future__ import annotations

import base64
import json
import math
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import settings                                  # noqa: E402

HDR, GRN, RED, YEL, DIM, OFF = ("\033[1m", "\033[32m", "\033[31m",
                                "\033[33m", "\033[2m", "\033[0m")
PHOTOS = pathlib.Path(__file__).resolve().parents[2] / "photos"
MODELS_URL = "https://api.replicate.com/v1/models/"
SEARCH_URL = "https://api.replicate.com/v1/models"

# The hard one on purpose. White rice on a white plate is where the colour rule
# returns nothing at all -- if SAM2 cannot do this photograph it cannot do the
# thing it is being switched on for.
PROBE_PHOTO = "26-potroast-rice-alone.jpg"
PROBE_GRAMS = 69

# How many search hits to read schemas for. Each is one GET; the search returns
# far more than is worth reading and the useful ones are at the top.
CANDIDATE_LIMIT = 25


def _pick_photo() -> tuple[str, int | None]:
    """Which photograph to probe. Default is the hardest one; any bench photo
    can be named by its number, because "does the fix work on THIS one" is a
    two-cent question and should not cost a bench run."""
    weights = {"17": 58, "18": 74, "20": 253, "21": 159, "22": 83, "23": 65,
               "24": 89, "25": 136, "26": 69, "27": 114, "28": 42, "29": 309,
               "30": 123, "31": 151, "32": 182, "33": 152, "34": 120,
               "35": 210, "36": 210}
    for a in sys.argv[1:]:
        key = a.strip().lstrip("0").zfill(2)
        if not key.isdigit():
            continue
        for f in sorted(PHOTOS.glob(f"{key}-*.jpg")):
            return f.name, weights.get(key)
    return PROBE_PHOTO, PROBE_GRAMS


def _secret(v: str) -> str:
    v = v or ""
    return f"{DIM}not set{OFF}" if not v else f"set, {len(v)} chars, starts {v[:4]}..."


def _settings_report() -> bool:
    print(f"\n{HDR}What is configured{OFF}")
    rows = [
        ("SEGMENTER_PROVIDER", settings.segmenter_provider or "", False),
        ("SEGMENTER_API_KEY", settings.segmenter_api_key or "", True),
        ("SEGMENTER_MODEL_VERSION", settings.segmenter_model_version or "", False),
        ("SEGMENTER_ENDPOINT", settings.segmenter_endpoint or "", False),
        ("SEGMENTER_IMAGE_FIELD", settings.segmenter_image_field or "", False),
        ("SEGMENTER_POINTS_FIELD", settings.segmenter_points_field or "", False),
        ("SEGMENTER_MODEL_INPUT", settings.segmenter_model_input or "", False),
        ("SEGMENTER_OUTPUT_FIELD", settings.segmenter_output_field or "", False),
    ]
    for name, value, secret in rows:
        shown = _secret(value) if secret else (value or f"{DIM}not set{OFF}")
        print(f"  {name:26s} {shown}")

    from app.services.ai import segment_hosted
    seg = segment_hosted.from_settings()
    live = type(seg).__name__ != "NullSegmenter"
    print(f"\n  built: {GRN if live else RED}{type(seg).__name__}{OFF}")
    if not live:
        print(f"  {YEL}Nothing will be measured.{OFF} {DIM}Every scan falls back to the "
              f"model's own area claim.{OFF}")
    return live


# Inputs that contain "point" and are NOT a point prompt.
#
# `points_per_side` is the automatic generator's sampling grid. It passes every
# naive test for a point input -- it has "point" in the name and it takes a
# number -- and it is the exact field that made meta/sam-2 look prompted when it
# is not. The call went out, was billed, and came back without the mask that was
# asked for. Any candidate whose only point-ish input is one of these is the
# automatic mask generator wearing a promising name.
AUTO_ONLY_INPUTS = {"points_per_side", "points_per_batch", "points_per_crop"}

# What a real point prompt is called, by publisher. The names vary; the shape
# does not -- a list of coordinates, usually with a parallel list of labels.
POINT_WORDS = ("point", "click", "coord")


def _point_inputs(props: dict) -> list[str]:
    """The inputs that could carry point COORDINATES. Empty means automatic."""
    return sorted(n for n in props
                  if n.lower() not in AUTO_ONLY_INPUTS
                  and any(w in n.lower() for w in POINT_WORDS))


def _input_props(body: dict) -> dict:
    schema = (body.get("latest_version") or {}).get("openapi_schema") or {}
    return (schema.get("components", {}).get("schemas", {})
            .get("Input", {}).get("properties", {}) or {})


def _schema(model: str) -> int:
    """Ask Replicate what this model actually takes. The integration sends an
    image field and a points field; if the model has no points input it is the
    automatic mask generator, not the prompted one, and the whole call shape
    is wrong."""
    import httpx
    key = settings.segmenter_api_key
    if not key:
        print(f"\n  {YEL}No API key, so the model's schema cannot be read.{OFF}")
        return 1
    print(f"\n{HDR}What {model} accepts{OFF}")
    try:
        r = httpx.get(MODELS_URL + model, timeout=20,
                      headers={"Authorization": f"Bearer {key}"})
    except Exception as exc:                                      # noqa: BLE001
        print(f"  {RED}could not reach Replicate: {type(exc).__name__}{OFF}")
        return 1
    if r.status_code >= 400:
        print(f"  {RED}HTTP {r.status_code}{OFF} {DIM}{r.text[:160]}{OFF}")
        return 1
    body = r.json()
    version = (body.get("latest_version") or {}).get("id") or ""
    props = (((body.get("latest_version") or {}).get("openapi_schema") or {})
             .get("components", {}).get("schemas", {})
             .get("Input", {}).get("properties", {}))
    if version:
        print(f"  latest version id: {version}")
    if not props:
        print(f"  {YEL}no input schema published{OFF}")
        return 1
    for name in sorted(props):
        p = props[name] or {}
        print(f"    {name:24s} {p.get('type', '?'):8s} {DIM}{str(p.get('description',''))[:60]}{OFF}")

    # SAID PLAINLY, because "is this the prompted one" is the question this
    # whole file exists to answer and it was previously left to the reader.
    coords = _point_inputs(props)
    auto_only = sorted(n for n in props if n.lower() in AUTO_ONLY_INPUTS)
    if coords:
        print(f"\n  {GRN}PROMPTED{OFF}  takes point coordinates: "
              f"{', '.join(coords)}")
    else:
        print(f"\n  {YEL}AUTOMATIC{OFF}  no coordinate input"
              + (f" -- only {', '.join(auto_only)}" if auto_only else ""))

    out = (((body.get("latest_version") or {}).get("openapi_schema") or {})
           .get("components", {}).get("schemas", {}).get("Output", {}))
    if out:
        print(f"\n{HDR}What it gives back{OFF}")
        # Output is often a bare type or an array; print whatever shape it is,
        # because the adapter has to read it and a guess here is a paid guess.
        kind = out.get("type", "?")
        print(f"    type: {kind}")
        if out.get("items"):
            print(f"    items: {json.dumps(out['items'])[:200]}")
        for name in sorted(out.get("properties", {})):
            q = out["properties"][name] or {}
            print(f"    {name:24s} {q.get('type', '?'):8s} "
                  f"{DIM}{str(q.get('description',''))[:60]}{OFF}")
        if not out.get("items") and not out.get("properties"):
            print(f"    {DIM}{json.dumps(out)[:300]}{OFF}")

    img = settings.segmenter_image_field
    pts = settings.segmenter_points_field
    auto = settings.segmenter_mode == "auto"
    print()
    print(f"  {GRN + 'ok  ' + OFF if img in props else RED + 'MISSING' + OFF}  "
          f"image field we send: {img!r}")
    if auto:
        print(f"  {GRN}ok  {OFF}  no points field is sent -- SEGMENTER_MODE=auto, "
              f"which is what this model is")
    else:
        ok = pts in props
        print(f"  {GRN + 'ok  ' + OFF if ok else RED + 'MISSING' + OFF}  "
              f"points field we send: {pts!r}")
        if not ok:
            print(f"  {YEL}This model has no point input. It is the automatic mask "
                  f"generator, not the prompted one.{OFF}")
            print(f"  {YEL}Set SEGMENTER_MODE=auto and it will be called the way "
                  f"it expects.{OFF}")
    if version and version != settings.segmenter_model_version:
        print(f"\n  {YEL}SEGMENTER_MODEL_VERSION does not match the latest "
              f"version above.{OFF}")
    return 0


def _candidates(term: str = "sam 2 segment") -> int:
    """Every SAM-ish model Replicate publishes, sorted into prompted and not.

    WHY THIS IS A COMMAND AND NOT A DECISION MADE ONCE BY READING

    The prompted model has not been chosen. It cannot be chosen from a search
    engine or from memory: what decides it is the input schema the publisher
    actually serves, and that is a thing to be read, not recalled. Wiring an
    integration against a guessed field name is precisely the failure this file
    was written after -- a call that succeeds, bills, and returns the wrong
    shape.

    Free. Schemas only, no predictions.
    """
    import httpx
    key = settings.segmenter_api_key
    if not key:
        print(f"\n  {YEL}No API key, so no schema can be read.{OFF}")
        return 1
    hdr = {"Authorization": f"Bearer {key}"}
    print(f"\n{HDR}Candidates for {term!r}{OFF}")
    try:
        r = httpx.request("QUERY", SEARCH_URL, timeout=30, headers={
            **hdr, "Content-Type": "text/plain"}, content=term)
    except Exception as exc:                                      # noqa: BLE001
        print(f"  {RED}could not reach Replicate: {type(exc).__name__}{OFF}")
        return 1
    if r.status_code >= 400:
        print(f"  {RED}HTTP {r.status_code}{OFF} {DIM}{r.text[:160]}{OFF}")
        return 1
    results = (r.json() or {}).get("results") or []
    if not results:
        print(f"  {YEL}nothing came back{OFF}")
        return 1

    prompted: list[tuple[str, str, str]] = []
    automatic: list[str] = []
    unknown: list[str] = []
    for m in results[:CANDIDATE_LIMIT]:
        slug = f"{m.get('owner','')}/{m.get('name','')}"
        props = _input_props(m)
        if not props:
            # Search results do not always carry the schema. Ask directly
            # rather than reporting "no point input" about a model we did not
            # actually read -- that mistake is how the wrong model gets ruled
            # out and stays ruled out.
            try:
                one = httpx.get(MODELS_URL + slug, timeout=20, headers=hdr)
                props = _input_props(one.json()) if one.status_code < 400 else {}
            except Exception:                                     # noqa: BLE001
                props = {}
        if not props:
            unknown.append(slug)
            continue
        coords = _point_inputs(props)
        if coords:
            version = (m.get("latest_version") or {}).get("id") or ""
            prompted.append((slug, ", ".join(coords), version))
        else:
            automatic.append(slug)

    print(f"\n  {GRN}PROMPTED -- takes point coordinates{OFF}")
    if prompted:
        for slug, fields, version in prompted:
            print(f"    {slug:44s} {fields}")
            if version:
                print(f"      {DIM}version {version}{OFF}")
    else:
        print(f"    {YEL}none{OFF}")
    print(f"\n  {DIM}automatic only: {', '.join(automatic) or 'none'}{OFF}")
    if unknown:
        print(f"  {DIM}no schema published: {', '.join(unknown)}{OFF}")
    print(f"\n  {DIM}Then: dev segcheck owner/model --schema-only{OFF}")
    return 0


def _probe() -> int:
    """One real call, on the photograph the colour rule cannot do."""
    name, grams = _pick_photo()
    globals()["PROBE_PHOTO"] = name
    globals()["PROBE_GRAMS"] = grams
    photo = PHOTOS / name
    if not photo.exists():
        print(f"\n  {YEL}{PROBE_PHOTO} not found, skipping the live call.{OFF}")
        return 1
    import numpy as np
    from PIL import Image, ImageOps
    from app.services.ai import segment_hosted

    seg = segment_hosted.from_settings()
    if type(seg).__name__ == "NullSegmenter":
        return 1
    im = ImageOps.exif_transpose(Image.open(photo)).convert("RGB")
    im.thumbnail((1280, 1280), Image.LANCZOS)
    arr = np.asarray(im)
    print(f"\n{HDR}One real call{OFF}  {DIM}{PROBE_PHOTO}" +
          (f" -- {PROBE_GRAMS} g weighed" if PROBE_GRAMS else "") + OFF)
    H, W = arr.shape[:2]

    # Seed on the PLATE, not on the middle of the photograph.
    #
    # The frame centre is not the food. These are portrait phone shots with the
    # plate high in the frame and a credit card below it; the middle pixel can
    # be table. Probing there and reporting "no mask" would blame the model for
    # a badly aimed question. The plate is found from pixels here, exactly as
    # the estimator finds it, and its centroid is the seed.
    from app.services.ai import food_seg
    seed = (W // 2, H // 2)
    where = "the middle of the frame (no plate found)"
    try:
        plate, _src = food_seg.plate_surface(arr, None)
        if plate is not None and plate.any():
            ys, xs = np.nonzero(plate)
            seed = (int(xs.mean()), int(ys.mean()))
            where = f"the centre of the plate, {plate.mean():.0%} of the frame"
    except Exception:                                             # noqa: BLE001
        pass
    print(f"  {DIM}seeded at {seed} -- {where}{OFF}")
    try:
        # One paid call. Everything below reads the masks it cached.
        seg.segment(arr, [seed])
    except Exception as exc:                                      # noqa: BLE001
        print(f"  {RED}raised {type(exc).__name__}: {exc}{OFF}")
        return 1
    # ----------------------------------------------------------------------
    # WHAT THE ESTIMATOR WOULD DO, THROUGH THE ESTIMATOR'S OWN CODE.
    #
    # An earlier version of this preview implemented its own version of the
    # selection rule. It reported 99.2% of the plate while production reported
    # 84.8% of the frame -- two different answers from two different rules,
    # neither of them checked against the other, and the bug reached a paid
    # bench run because the only code exercised on REAL masks was the one that
    # did not ship. There is one entry point now: segment_boxes, the same call
    # the estimator makes.
    #
    # And it is run across a RANGE of box sizes, because the model's boxes land
    # on a 0.05 grid and have been observed at 1%, 4%, 16% and 25% of frame for
    # the same food between runs. A rule that is right at one box size and
    # wrong at the next is not a measurement, and that has now cost two bench
    # runs. If these rows disagree, stop.
    # ----------------------------------------------------------------------
    found = getattr(seg, "_auto_memo", None)
    if found and found[1]:
        sizes = sorted(float(m.mean()) for m in found[1])
        print(f"  {len(sizes)} mask(s) returned, covering: " +
              ", ".join(f"{v:.1%}" for v in sizes))
        out = _overlay(arr, found[1], seed)
        if out:
            print(f"  {DIM}drawn: {out}{OFF}")
        npz = _save_masks(found[1], seed)
        if npz:
            print(f"  {DIM}masks saved: {npz}  (replayable offline, no further "
                  f"calls){OFF}")

    plate_ratio = float(plate.mean()) if plate is not None and plate.any() else 0.0
    print(f"\n  {HDR}the estimator's own rule, across the box sizes it sees{OFF}")
    print(f"    {'box':>7s} {'masks':>6s} {'dropped':>8s} {'footprint':>10s} "
          f"{'of plate':>9s} {'implied h':>10s}")
    H, W = arr.shape[:2]
    seen = []
    for frac in (0.04, 0.09, 0.16, 0.25):
        side = math.sqrt(frac)
        bw, bh = side, side
        box = {"x": max(0.0, seed[0] / W - bw / 2), "y": max(0.0, seed[1] / H - bh / 2),
               "w": bw, "h": bh}
        try:
            res = seg.segment_boxes(arr, [seed], [box])
        except Exception as exc:  # noqa: BLE001
            print(f"    {frac:6.0%} raised {type(exc).__name__}")
            continue
        if not res or res[0] is None:
            print(f"    {frac:6.0%} {'--':>6s} {'--':>8s} {'no mask':>10s}")
            continue
        m = res[0].mask
        area = float(m.mean())
        of_plate = (float((m & plate).sum()) / max(int(plate.sum()), 1)
                    if plate is not None and plate.any() else float("nan"))
        h = ""
        if PROBE_GRAMS and of_plate == of_plate and of_plate > 0:
            mm2 = of_plate * math.pi * (229.0 / 2) ** 2
            h = f"{PROBE_GRAMS * 1000.0 / mm2:8.1f}mm"
        colour = RED if area > 0.5 else GRN
        print(f"    {frac:6.0%} {'':>6s} {'':>8s} {colour}{area:9.1%}{OFF} "
              f"{of_plate:8.1%} {h:>10s}")
        seen.append(area)
    if len(seen) >= 2:
        # THE TEST IS SATURATION, NOT SAMENESS. A small box genuinely holds
        # fewer of the pieces -- four of ten carrots is the honest answer to a
        # box that only covers four of them -- so the footprint SHOULD grow
        # while the box is still smaller than the food. What must happen is
        # that it stops growing once the box contains the item: grow, then
        # plateau. Still climbing at the widest box means the box is admitting
        # things that are not the food, which is the failure that has now cost
        # two bench runs.
        tail = seen[-2:]
        drift = max(tail) / max(min(tail), 1e-9)
        if drift <= 1.15:
            print(f"    {GRN}plateaus{OFF} -- the footprint stops growing once "
                  f"the box holds the food, which is what it should do")
        else:
            print(f"    {RED}still climbing at the widest box ({drift:.1f}x "
                  f"between the last two){OFF} -- the box is letting in "
                  f"something that is not the food")
            return 1
    if any(a > 0.5 for a in seen):
        print(f"    {RED}a footprint over half the frame is the plate or the "
              f"table, not the food.{OFF}")
        return 1
    return 0


def _overlay(rgb, masks, seed) -> str | None:
    """Draw every returned mask over the photograph, one colour each.

    Looking at it is not optional. Every wrong turn on this project that cost a
    day was a number believed without a picture behind it.
    """
    try:
        import numpy as np
        from PIL import Image
        colours = [(255, 60, 60), (60, 160, 255), (60, 220, 120), (255, 200, 40),
                   (220, 80, 255), (0, 220, 220), (255, 140, 0), (150, 150, 255)]
        canvas = rgb.astype(np.float32).copy()
        for i, m in enumerate(sorted(masks, key=lambda m: -int(m.sum()))):
            c = np.array(colours[i % len(colours)], np.float32)
            canvas[m] = canvas[m] * 0.55 + c * 0.45
        y, x = int(seed[1]), int(seed[0])
        canvas[max(0, y - 12):y + 12, max(0, x - 2):x + 2] = (255, 255, 255)
        canvas[max(0, y - 2):y + 2, max(0, x - 12):x + 12] = (255, 255, 255)
        folder = PHOTOS.parent / "mask_overlays"
        folder.mkdir(exist_ok=True)
        path = folder / f"segcheck-{PROBE_PHOTO.rsplit('.', 1)[0]}.png"
        Image.fromarray(canvas.astype("uint8")).save(path)
        return str(path)
    except Exception:  # noqa: BLE001
        return None


def _save_masks(masks, seed) -> str | None:
    """Every returned mask, replayable offline.

    A paid call should buy a permanent fixture, not a one-off look. These are
    the real thing -- a plate with food-shaped holes punched in it, which no
    synthetic disc reproduces, and which is exactly what walked past a guard
    that 456 green tests had blessed.
    """
    try:
        import numpy as np
        folder = PHOTOS.parent / "mask_overlays"
        folder.mkdir(exist_ok=True)
        path = folder / f"masks-{PROBE_PHOTO.rsplit('.', 1)[0]}.npz"
        np.savez_compressed(path, seed=np.asarray(seed),
                            **{f"m{i}": m for i, m in enumerate(masks)})
        return str(path)
    except Exception:  # noqa: BLE001
        return None


def _tuning() -> dict:
    """Model knobs from the command line, so a sweep is not eight .env edits.

    SAM2's automatic generator filters its own masks by confidence and
    stability. On a blown-out white plate those scores are low and the plate is
    discarded before it ever reaches us, which no amount of arguing with the
    defaults will change -- only measuring will. These override
    SEGMENTER_MODEL_INPUT for one run.
    """
    out: dict = {}
    args = sys.argv[1:]
    for flag, key, cast in (("--pps", "points_per_side", int),
                            ("--iou", "pred_iou_thresh", float),
                            ("--stability", "stability_score_thresh", float)):
        if flag in args:
            i = args.index(flag)
            if i + 1 < len(args):
                try:
                    out[key] = cast(args[i + 1])
                except ValueError:
                    print(f"  {YEL}{flag} needs a number, ignoring{OFF}")
    return out


def main() -> int:
    print(f"\n{HDR}NeutriAI -- segmenter check{OFF}")
    print(f"{DIM}  Keys are never printed. One paid call at the end, about 2 cents.{OFF}")
    tune = _tuning()
    if tune:
        settings.segmenter_model_input = json.dumps(tune)
        print(f"\n{HDR}Overridden for this run{OFF}\n  " +
              "  ".join(f"{k}={v}" for k, v in tune.items()))
    args = sys.argv[1:]
    if "--candidates" in args:
        i = args.index("--candidates")
        term = args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-") \
            else "sam 2 segment"
        return _candidates(term)
    live = _settings_report()
    model = next((a for a in args if "/" in a), "meta/sam-2").strip()
    # Both of these already compute the right code -- "no API key", "could not
    # reach Replicate", "still climbing at the widest box" -- and both were
    # thrown away, so every failure exited 0 and nothing could gate on this.
    rc = _schema(model)
    # A schema read of a CANDIDATE must not fire a paid call at whatever is
    # configured. Interrogating a model we are thinking about and paying the
    # one we already have are different acts.
    if "--schema-only" in args:
        print(f"\n{DIM}  --schema-only: no call made.{OFF}")
    elif live:
        rc = _probe() or rc
    else:
        print(f"\n{DIM}  No live call: nothing is configured to call.{OFF}")
    print()
    return rc


if __name__ == "__main__":
    sys.exit(main())
