"""The depth model called over HTTP.

No network: every response is served by a stubbed transport, so these run
offline and deterministically. What they are actually guarding is the defect
this project keeps finding -- something built, tested, and never connected. The
last test in this file takes a ray-traced plate, serves it through the provider
exactly as an endpoint would, and checks a real height comes out the far end.
"""
from __future__ import annotations

import base64
import io
import json

import httpx
import numpy as np
import pytest

from app.services.ai import depth_hosted as H
from app.services.ai import depth_map as D
from tests.test_depth_map import PLATE_MM, _scene

PHOTO = np.zeros((240, 320, 3), dtype=np.uint8)
PHOTO[60:180, 80:240] = 200


# --- helpers ----------------------------------------------------------------

def _png(arr: np.ndarray, mode: str = "L") -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    if mode == "I;16":
        Image.fromarray(arr.astype(np.uint16)).save(buf, format="PNG")
    elif mode == "RGB":
        Image.fromarray(arr.astype(np.uint8), mode="RGB").save(buf, format="PNG")
    else:
        Image.fromarray(arr.astype(np.uint8), mode="L").save(buf, format="PNG")
    return buf.getvalue()


def _grey(h=120, w=160) -> np.ndarray:
    ys, xs = np.mgrid[0:h, 0:w]
    return (ys / max(h - 1, 1) * 255).astype(np.uint8)


def _colour_mapped(h=120, w=160) -> np.ndarray:
    """A depth map rendered through a colour map, the way most endpoints do.

    Deliberately a real one in spirit: dark at BOTH ends, bright in the middle,
    which is what makes greying it out non-monotonic and the resulting heights
    meaningless.
    """
    ys, _ = np.mgrid[0:h, 0:w]
    v = ys / max(h - 1, 1)
    r = np.clip(1.5 - abs(v - 0.75) * 4, 0, 1)
    g = np.clip(1.5 - abs(v - 0.50) * 4, 0, 1)
    b = np.clip(1.5 - abs(v - 0.25) * 4, 0, 1)
    return (np.dstack([r, g, b]) * 255).astype(np.uint8)


def _provider(handler, **kw):
    opts = dict(dialect="replicate", api_key="k-secret", version="v1",
                timeout_s=2.0, transport=httpx.MockTransport(handler))
    opts.update(kw)
    return H.HostedDepth(**opts)


def _succeeded(output):
    return httpx.Response(201, json={"id": "p1", "status": "succeeded", "output": output,
                                     "urls": {"get": "https://api.replicate.com/v1/predictions/p1"}})


# --- the contract -----------------------------------------------------------

def test_a_prediction_that_finishes_on_the_first_call_is_used():
    """Replicate's `Prefer: wait` holds the connection until the model is done,
    which is one round trip for a model that runs in about two seconds."""
    seen = {}

    def handler(request):
        if request.url.host == "api.replicate.com":
            seen["prefer"] = request.headers.get("Prefer")
            seen["body"] = json.loads(request.content)
            return _succeeded("https://cdn.example/depth.png")
        return httpx.Response(200, content=_png(_grey()),
                              headers={"content-type": "image/png"})

    got = _provider(handler).depth(PHOTO)
    assert got is not None and got.shape == PHOTO.shape[:2]
    assert seen["prefer"] == "wait"
    assert seen["body"]["version"] == "v1"
    assert seen["body"]["input"]["image"].startswith("data:image/jpeg;base64,")


def test_a_queued_prediction_is_polled_until_it_finishes():
    calls = {"n": 0}

    def handler(request):
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=_png(_grey()),
                                  headers={"content-type": "image/png"})
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(201, json={
                "id": "p1", "status": "starting", "output": None,
                "urls": {"get": "https://api.replicate.com/v1/predictions/p1"}})
        return httpx.Response(200, json={"id": "p1", "status": "succeeded",
                                         "output": ["https://cdn.example/d.png"]})

    assert _provider(handler).depth(PHOTO) is not None
    assert calls["n"] >= 2


def test_a_prediction_still_queued_at_the_deadline_gives_up_quietly():
    """The scan is waiting on this. A model that has not answered by the
    deadline costs a measurement, and the portion falls back to its prior."""
    def handler(request):
        return httpx.Response(201, json={
            "id": "p1", "status": "processing", "output": None,
            "urls": {"get": "https://api.replicate.com/v1/predictions/p1"}})

    assert _provider(handler, timeout_s=1.0).depth(PHOTO) is None


@pytest.mark.parametrize("status", ["failed", "canceled"])
def test_a_failed_prediction_is_not_read_as_a_depth_map(status):
    def handler(request):
        return httpx.Response(201, json={"id": "p1", "status": status,
                                         "output": None, "error": "boom"})
    assert _provider(handler).depth(PHOTO) is None


@pytest.mark.parametrize("code", [401, 402, 429, 500])
def test_an_http_error_costs_a_measurement_not_a_scan(code):
    """Out of credit, rate limited, or the vendor is down. None of it may reach
    the caller as an exception -- the photograph still has to be scanned."""
    def handler(request):
        return httpx.Response(code, text="nope")
    assert _provider(handler).depth(PHOTO) is None


def test_a_transport_that_throws_is_survived():
    def handler(request):
        raise httpx.ConnectError("no route to host")
    assert _provider(handler).depth(PHOTO) is None


# --- the refusal that matters most ------------------------------------------

def test_a_colour_mapped_depth_image_is_refused():
    """THE most likely way to wire this up wrong.

    Most depth endpoints return a pretty inferno or spectral rendering by
    default. It is a valid PNG of the right size and it decodes cleanly, so
    nothing downstream can tell. But a colour map is dark at both ends of the
    scale, so greying it makes depth a NON-MONOTONIC function of itself: the
    near food and the far tablecloth read the same, the plane fit lands
    anywhere, and every height is invented.
    """
    def handler(request):
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=_png(_colour_mapped(), "RGB"),
                                  headers={"content-type": "image/png"})
        return _succeeded("https://cdn.example/pretty.png")
    assert _provider(handler).depth(PHOTO) is None


def test_a_grey_map_carried_in_three_channels_is_still_accepted():
    """Refusing colour must not refuse grey. Plenty of endpoints return a
    single-channel map encoded as RGB, and that is a real depth map."""
    grey = _grey()
    def handler(request):
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=_png(np.dstack([grey] * 3), "RGB"),
                                  headers={"content-type": "image/png"})
        return _succeeded("https://cdn.example/d.png")
    got = _provider(handler).depth(PHOTO)
    assert got is not None
    assert got.max() > got.min()


def test_the_colour_check_reads_the_channels_not_the_file_type():
    assert H.is_colour_mapped(np.dstack([np.zeros((4, 4))] * 3)) is False
    rgb = np.dstack([np.ones((4, 4)), np.zeros((4, 4)), np.zeros((4, 4))])
    assert H.is_colour_mapped(rgb) is True


# --- what comes back --------------------------------------------------------

def test_a_sixteen_bit_map_keeps_its_resolution():
    """An 8-bit map has 256 levels across the whole scene, and the relief this
    app is measuring is a few millimetres of it. 16 bits is what a careful
    endpoint returns and it must survive the decode."""
    fine = (np.mgrid[0:120, 0:160][0] * 500).astype(np.uint16)
    def handler(request):
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=_png(fine, "I;16"),
                                  headers={"content-type": "image/png"})
        return _succeeded("https://cdn.example/d.png")
    got = _provider(handler).depth(PHOTO)
    assert got is not None
    assert got.max() > 5000, got.max()      # not squashed into 0..255


def test_a_map_of_the_wrong_size_is_resampled_rather_than_dropped():
    """The measurement needs the depth map and the plate mask to be the same
    shape. A model returning its own resolution would otherwise be called,
    billed, and silently ignored -- which is this project's oldest bug."""
    def handler(request):
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=_png(_grey(77, 91)),
                                  headers={"content-type": "image/png"})
        return _succeeded("https://cdn.example/d.png")
    got = _provider(handler).depth(PHOTO)
    assert got is not None and got.shape == PHOTO.shape[:2]


def test_a_data_uri_and_bare_base64_are_both_understood():
    payload = base64.b64encode(_png(_grey())).decode()
    for output in ("data:image/png;base64," + payload, payload):
        def handler(request, output=output):
            return _succeeded(output)
        assert _provider(handler).depth(PHOTO) is not None


def test_rubbish_where_a_depth_map_should_be_is_refused():
    for output in (None, 42, {"nothing": "useful"}, "not base64 at all !!!"):
        def handler(request, output=output):
            return _succeeded(output)
        assert _provider(handler).depth(PHOTO) is None


# --- the credential ---------------------------------------------------------

def test_the_model_key_is_never_sent_to_the_output_host():
    """The prediction is authorised; the file it produced is served from object
    storage that neither needs nor should see the key."""
    seen = {}
    def handler(request):
        if request.url.host == "cdn.example":
            seen["cdn_auth"] = request.headers.get("Authorization")
            return httpx.Response(200, content=_png(_grey()),
                                  headers={"content-type": "image/png"})
        seen["api_auth"] = request.headers.get("Authorization")
        return _succeeded("https://cdn.example/d.png")
    _provider(handler).depth(PHOTO)
    assert seen["api_auth"] == "Bearer k-secret"
    assert seen["cdn_auth"] is None


# --- the plain endpoint -----------------------------------------------------

def test_a_plain_endpoint_returning_an_image_works():
    def handler(request):
        assert request.headers["Content-Type"] == "image/jpeg"
        return httpx.Response(200, content=_png(_grey()),
                              headers={"content-type": "image/png"})
    p = H.HostedDepth(dialect="http", endpoint="https://depth.internal/predict",
                      transport=httpx.MockTransport(handler))
    assert p.depth(PHOTO) is not None


def test_a_plain_endpoint_returning_json_works():
    payload = base64.b64encode(_png(_grey())).decode()
    def handler(request):
        return httpx.Response(200, json={"depth": payload})
    p = H.HostedDepth(dialect="http", endpoint="https://depth.internal/predict",
                      transport=httpx.MockTransport(handler))
    assert p.depth(PHOTO) is not None


# --- configuration ----------------------------------------------------------

def test_the_model_s_own_input_names_are_config_not_code():
    """Which weights, which encoder, what the image field is called -- all of it
    belongs in the environment, because the thing that actually matters about
    the model is its LICENCE and that follows the weights, not the vendor."""
    seen = {}
    def handler(request):
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=_png(_grey()),
                                  headers={"content-type": "image/png"})
        seen["input"] = json.loads(request.content)["input"]
        return _succeeded("https://cdn.example/d.png")
    _provider(handler, image_field="input_image",
              extra_input={"encoder": "vits"}).depth(PHOTO)
    assert seen["input"]["encoder"] == "vits"
    assert "input_image" in seen["input"]


def test_an_unconfigured_provider_measures_nothing_and_never_calls_out():
    def handler(request):          # pragma: no cover - must never run
        raise AssertionError("an unconfigured provider made a network call")
    for kw in (dict(dialect=""), dict(dialect="replicate", api_key="", version="v"),
               dict(dialect="replicate", api_key="k", version=""),
               dict(dialect="http", endpoint="")):
        p = H.HostedDepth(transport=httpx.MockTransport(handler), **kw)
        assert p.available() is False
        assert p.depth(PHOTO) is None


def _configure(monkeypatch, **kw):
    """Settings for a configured Replicate provider, overriding conftest's pins."""
    from app.config import settings
    base = dict(depth_provider="replicate", depth_api_key="k-secret",
                depth_model_version="v1", depth_model_size="Small",
                depth_model_input="", depth_output_field="",
                depth_image_field="image", depth_endpoint="", depth_timeout_s=2.0)
    base.update(kw)
    for key, value in base.items():
        monkeypatch.setattr(settings, key, value, raising=False)


def _never(request):  # pragma: no cover - must never run
    raise AssertionError("a provider that should be off made a network call")


@pytest.mark.parametrize("size", ["", "Large", "Base", "Giant", "vitl"])
def test_depth_stays_off_unless_the_licensed_small_model_is_declared(monkeypatch, size):
    """The model size is a LICENCE. Depth Anything V2 Small is Apache-2.0; Base,
    Large and Giant are CC-BY-NC and cannot serve a paid app -- and the Replicate
    model first used defaults to Large. Omitting the size must switch depth off,
    never fall through to whatever the endpoint defaults to."""
    _configure(monkeypatch, depth_model_size=size)
    provider = H.from_settings(transport=httpx.MockTransport(_never))
    assert provider.available() is False
    assert provider.depth(PHOTO) is None


def test_small_is_sent_on_every_prediction(monkeypatch):
    """Declared once in config, sent explicitly on the wire, so the endpoint's own
    default can never decide which weights run."""
    seen = {}
    def handler(request):
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=_png(_grey()),
                                  headers={"content-type": "image/png"})
        seen["input"] = json.loads(request.content)["input"]
        return _succeeded("https://cdn.example/d.png")
    _configure(monkeypatch)
    provider = H.from_settings(transport=httpx.MockTransport(handler))
    assert provider.available() is True
    assert provider.depth(PHOTO) is not None
    assert seen["input"]["model_size"] == "Small"


def test_a_size_smuggled_through_the_free_form_input_switches_depth_off(monkeypatch):
    _configure(monkeypatch, depth_model_input=json.dumps({"model_size": "Large"}))
    assert H.from_settings(transport=httpx.MockTransport(_never)).available() is False


def test_the_grey_map_is_read_only_when_it_is_named(monkeypatch):
    """chenxwh/depth-anything-v2 answers {"grey_depth": url, "color_depth": url}
    (checked against the model's schema and one real Small prediction). Named,
    the grey map is used; unnamed, the answer is dropped unread rather than
    guessed at -- and the colour one is refused either way."""
    def handler(request):
        if request.url.host == "cdn.example":
            if request.url.path.endswith("grey.png"):
                return httpx.Response(200, content=_png(_grey()),
                                      headers={"content-type": "image/png"})
            return httpx.Response(200, content=_png(_colour_mapped(), mode="RGB"),
                                  headers={"content-type": "image/png"})
        return _succeeded({"grey_depth": "https://cdn.example/grey.png",
                           "color_depth": "https://cdn.example/colour.png"})
    _configure(monkeypatch, depth_output_field="grey_depth")
    assert H.from_settings(transport=httpx.MockTransport(handler)).depth(PHOTO) is not None
    _configure(monkeypatch, depth_output_field="color_depth")
    assert H.from_settings(transport=httpx.MockTransport(handler)).depth(PHOTO) is None
    _configure(monkeypatch, depth_output_field="")
    assert H.from_settings(transport=httpx.MockTransport(handler)).depth(PHOTO) is None


def test_the_default_is_no_depth_at_all():
    """Nothing is configured out of the box, and the app behaves exactly as it
    does today: no heights measured, every portion on its prior."""
    from app.services.ai import vision
    assert H.from_settings().available() is False
    assert vision.DEPTH_PROVIDER.available() is False


def test_a_metric_provider_is_refused_rather_than_measured_wrongly():
    """depth_map's geometry is written for a RELATIVE INVERSE map and resolves
    its gain from the plate. A metric map -- LiDAR, ARKit -- is millimetres
    already and needs different arithmetic that is not built. Declared and
    checked, so that path cannot be wired up quietly wrong."""
    from app.services.ai import vision

    class Metric:
        name, units = "lidar", "mm"
        def available(self): return True
        def depth(self, rgb): raise AssertionError("must not be called")

    depth, plate, food = _scene("hemisphere")
    old = vision.DEPTH_PROVIDER
    try:
        vision.DEPTH_PROVIDER = Metric()
        assert vision._measured_heights([{"bbox": {}}], b"x", {"x": 0}, PLATE_MM) == [None]
    finally:
        vision.DEPTH_PROVIDER = old


# --- the whole wire ---------------------------------------------------------

def test_a_ray_traced_plate_served_over_http_measures_the_right_height():
    """The end-to-end proof, and the reason this file exists.

    A plate of food is ray-traced, encoded as the 16-bit PNG a careful endpoint
    would return, served through the provider, decoded, and handed to the
    measurement. A 20.0 mm mean height has to come out -- which says the upload,
    the decode, the resample and the geometry all agree on what the numbers
    mean. Every part of that chain has passed its own test before now, and this
    project's most expensive bugs have all lived in the joins.
    """
    depth, plate, food = _scene("hemisphere", peak_mm=30.0)   # true mean 20.0 mm
    as_16bit = (depth * 65535).astype(np.uint16)

    def handler(request):
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=_png(as_16bit, "I;16"),
                                  headers={"content-type": "image/png"})
        return _succeeded("https://cdn.example/d.png")

    photo = np.zeros((depth.shape[0], depth.shape[1], 3), dtype=np.uint8)
    returned = _provider(handler).depth(photo)
    assert returned is not None and returned.shape == depth.shape

    got = D.measure_heights(returned, plate, [food], PLATE_MM)[0]
    assert got is not None, "the round trip lost the measurement entirely"
    assert got == pytest.approx(20.0, rel=0.10), got


# --- the plate has to have been measured, not drawn ---------------------------

def test_a_box_derived_plate_reports_the_shape_of_the_camera_not_the_plate():
    """The exact artifact this guard exists for, reproduced.

    `surface_mask` given the model's plate box draws an AXIS-ALIGNED ellipse
    whose semi-axes are the box's width and height in PIXELS. For a box that is
    roughly square in normalised coordinates, those pixel axes are in the ratio
    of the image itself -- so the "plate" comes back at arccos(3/4) = 41.4
    degrees on every 3:4 photograph ever taken.

    A tilt survey over sixteen bench photographs reported eight of them at
    41.4 degrees to one decimal place. This test is that survey, in four lines.
    """
    from app.services.ai import depth_map as D
    from app.services.ai import food_seg

    frame = np.zeros((1200, 900, 3), dtype=np.uint8)          # a 3:4 photograph
    box = {"x": 0.10, "y": 0.10, "w": 0.80, "h": 0.80}        # square in the box's terms
    mask, source = food_seg.plate_surface(frame, box)
    assert source == "box"

    major, minor = D.plate_axes_px(mask)
    tilt = np.degrees(np.arccos(min(1.0, minor / major)))
    assert tilt == pytest.approx(41.4, abs=1.0), (
        f"the artifact changed shape: {tilt:.1f} deg")
    assert tilt == pytest.approx(
        np.degrees(np.arccos(900 / 1200)), abs=1.0), "and it is the frame, not the plate"


def test_a_plate_that_was_never_measured_yields_no_height():
    """So the number above can never be published as a measurement.

    Built so the ONLY difference between the two halves is the provenance
    label. Same ray-traced plate, same depth map, same food: called "pixels" it
    measures 20 mm, called "box" it measures nothing. An earlier version of
    this test passed with the guard deleted -- it was failing on the
    orientation check instead, and proved only that zeros are not a depth map.
    """
    from app.services.ai import food_seg, vision
    from PIL import Image

    depth, plate, food = _scene("hemisphere", peak_mm=30.0)

    class Served:
        name, units = "stub", "relative_inverse"
        def available(self): return True
        def depth(self, rgb): return depth

    png = io.BytesIO()
    Image.fromarray(np.zeros(depth.shape + (3,), dtype=np.uint8)).save(png, "PNG")
    raw = png.getvalue()
    dets = [{"bbox": {"x": 0.3, "y": 0.3, "w": 0.4, "h": 0.4}}]

    real_surface, real_items = food_seg.plate_surface, food_seg.item_masks
    old = vision.DEPTH_PROVIDER
    try:
        vision.DEPTH_PROVIDER = Served()
        food_seg.item_masks = lambda rgb, boxes, pb=None: [food]

        food_seg.plate_surface = lambda rgb, bbox=None: (plate, "pixels")
        measured = vision._measured_heights(dets, raw, {"x": 0.1}, PLATE_MM)

        food_seg.plate_surface = lambda rgb, bbox=None: (plate, "box")
        drawn = vision._measured_heights(dets, raw, {"x": 0.1}, PLATE_MM)
    finally:
        food_seg.plate_surface, food_seg.item_masks = real_surface, real_items
        vision.DEPTH_PROVIDER = old

    assert measured[0] is not None, "the measured plate should have produced a height"
    assert measured[0] == pytest.approx(20.0, rel=0.15), measured
    assert drawn == [None], f"a box-derived plate was measured anyway: {drawn}"


def test_the_fence_still_works_exactly_as_it_did():
    """`surface_mask` is what every other caller uses and it must not have
    moved: they want somewhere to look, not a shape to measure."""
    from app.services.ai import food_seg

    frame = np.zeros((400, 300, 3), dtype=np.uint8)
    box = {"x": 0.1, "y": 0.1, "w": 0.8, "h": 0.8}
    assert np.array_equal(food_seg.surface_mask(frame, box),
                          food_seg.plate_surface(frame, box)[0])
    assert food_seg.surface_mask(frame, {"x": "junk", "y": 0, "w": 1, "h": 1}) is None
