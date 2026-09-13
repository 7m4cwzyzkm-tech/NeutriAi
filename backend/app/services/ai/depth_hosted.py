"""The depth model, called over HTTP.

WHY HOSTED RATHER THAN LOCAL

Running the weights in-process means torch in the API image -- roughly 2 GB of
container, a cold start measured in tens of seconds, and a GPU bill that is
paid whether anyone is scanning or not. Hosted inference is an HTTP contract
against a warm endpoint, it matches how every other model in this app is
already called, and the choice of vendor stays a config value.

WHY THE DIALECT IS CONFIGURABLE AND THE VENDOR IS NOT IN THE CODE

The thing that actually matters about the model is its LICENCE. Depth Anything
V2 Small is Apache-2.0 and can serve a paid app; the Base, Large and Giant
weights are CC-BY-NC and cannot. That constraint follows the weights, not the
host, so which host serves them is deliberately a setting -- and the model's own
input names ("image", "encoder": "vits", and so on) are config too, so swapping
endpoints is an env change rather than an edit here.

WHAT THIS FILE REFUSES TO DO, AND WHY EACH REFUSAL EXISTS

  * A COLOUR-MAPPED depth image. Most depth endpoints return a pretty inferno
    or spectral visualisation by default. Turned into grey it is a NON-MONOTONIC
    function of depth -- dark red and dark blue are the two ends of the scale
    and both come out dark. It looks like a depth map, it decodes like a depth
    map, and every height built on it is nonsense. This is the single most
    likely way to wire this up wrong, so it is checked on every response.

  * A map that is not the shape of the photograph. Resized here rather than
    left for the measurement to reject, because a silent shape mismatch would
    mean the model is called, billed, and never used -- which is the exact
    defect class this project keeps finding.

  * Anything at all, on any error. A depth model that fails costs a MEASUREMENT,
    never a scan: heights fall back to their priors and the estimate is the one
    the app would have produced anyway.
"""
from __future__ import annotations

import base64
import io
import json
import time

import httpx
import numpy as np
import structlog

from ...config import settings

from .depth_map import DepthProvider
log = structlog.get_logger()

# What the model is sent. Downscaled because the depth of a plate is a smooth
# field -- there is nothing in the last octave worth the payload -- and the map
# is resampled to the photograph's own size on the way back.
MAX_UPLOAD_PX = 1024
UPLOAD_QUALITY = 90

# How achromatic a three-channel response has to be to be believed as a depth
# map rather than a colour-mapped picture of one. A real grey image encoded as
# RGB has a channel spread of exactly zero; JPEG ringing puts it at a level or
# two; the mildest colour map is off the scale.
MAX_CHANNEL_SPREAD = 6.0 / 255.0

# The only model size this app may run. Depth Anything V2 Small is Apache-2.0;
# Base, Large and Giant are CC-BY-NC-4.0. Required in config -- see from_settings.
LICENSED_MODEL_SIZE = "Small"

REPLICATE_URL = "https://api.replicate.com/v1/predictions"
# Verified against Replicate's own documentation rather than assumed: the
# terminal states are these, and "successful" -- which reads more naturally and
# is what I first wrote -- is not one of them.
REPLICATE_DONE = {"succeeded", "failed", "canceled", "aborted"}


# ---------------------------------------------------------------------------
# the photograph on its way out
# ---------------------------------------------------------------------------

def encode_image(rgb: np.ndarray) -> bytes | None:
    """The photograph as JPEG bytes, downscaled for the wire."""
    try:
        from PIL import Image
    except Exception:  # pragma: no cover - Pillow is a hard dependency
        return None
    if rgb is None or getattr(rgb, "ndim", 0) != 3:
        return None
    img = Image.fromarray(np.asarray(rgb).astype(np.uint8), "RGB")
    img.thumbnail((MAX_UPLOAD_PX, MAX_UPLOAD_PX), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=UPLOAD_QUALITY)
    return buf.getvalue()


def data_uri(raw: bytes) -> str:
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------------------
# the depth map on its way back
# ---------------------------------------------------------------------------

def is_colour_mapped(arr: np.ndarray) -> bool:
    """Is this a depth map, or a picture of one?

    A colour map is the failure that does not announce itself. Every endpoint
    that renders one produces a perfectly valid image of the right size, and
    turning it grey gives a number that rises, falls and rises again with depth
    -- so near and far food read the same and the plane fit lands anywhere.
    """
    if arr is None or arr.ndim != 3 or arr.shape[2] < 3:
        return False
    chans = arr[:, :, :3].astype(np.float64)
    spread = float(np.mean(chans.max(axis=2) - chans.min(axis=2)))
    return spread > MAX_CHANNEL_SPREAD


def decode_depth(raw: bytes, shape: tuple[int, int]) -> np.ndarray | None:
    """Bytes from the endpoint to a float depth field the size of the photo.

    Accepts a 16-bit PNG (what a careful endpoint returns), an 8-bit grey image,
    a grey image carried in three channels, or a raw .npy array. Refuses a
    colour-mapped one.
    """
    if not raw:
        return None
    arr = None

    if raw[:6] in (b"\x93NUMPY",):
        try:
            arr = np.load(io.BytesIO(raw), allow_pickle=False)
        except Exception as exc:  # noqa: BLE001
            log.warning("depth_npy_unreadable", error=str(exc)[:120])
            return None
    else:
        try:
            from PIL import Image
            img = Image.open(io.BytesIO(raw))
            img.load()
        except Exception as exc:  # noqa: BLE001
            log.warning("depth_image_unreadable", error=str(exc)[:120])
            return None
        if img.mode in ("RGB", "RGBA", "P"):
            probe = np.asarray(img.convert("RGB"), dtype=np.float64) / 255.0
            if is_colour_mapped(probe):
                log.warning("depth_colour_mapped_rejected", mode=img.mode)
                return None
            arr = probe[:, :, 0]
        else:
            # I;16, I, F and L all carry one channel and keep their range.
            arr = np.asarray(img, dtype=np.float64)

    if arr is None:
        return None
    arr = np.asarray(arr, dtype=np.float64)
    if arr.ndim == 3 and arr.shape[2] in (1, 3, 4):
        arr = arr[:, :, 0]
    if arr.ndim != 2 or arr.size == 0 or not np.all(np.isfinite(arr)):
        log.warning("depth_array_unusable", shape=getattr(arr, "shape", None))
        return None

    return resample(arr, shape)


def resample(arr: np.ndarray, shape: tuple[int, int]) -> np.ndarray | None:
    """The map at the photograph's own size.

    Not optional and not the caller's problem: the measurement requires the
    depth map and the plate mask to be the same array shape, so a model that
    returns its own resolution would be called, paid for, and silently ignored.
    """
    h, w = int(shape[0]), int(shape[1])
    if h <= 0 or w <= 0:
        return None
    if arr.shape[:2] == (h, w):
        return arr
    try:
        from PIL import Image
        lo, hi = float(arr.min()), float(arr.max())
        span = hi - lo
        norm = (arr - lo) / span if span > 0 else np.zeros_like(arr)
        img = Image.fromarray(norm.astype(np.float32), mode="F").resize((w, h), Image.BILINEAR)
        # Put the original range back, so a caller reading raw values still sees
        # the model's own numbers rather than a 0..1 rescale.
        return np.asarray(img, dtype=np.float64) * span + lo
    except Exception as exc:  # noqa: BLE001
        log.warning("depth_resample_failed", error=str(exc)[:120])
        return None


# ---------------------------------------------------------------------------
# the provider
# ---------------------------------------------------------------------------

class HostedDepth:
    """A depth model behind an HTTP endpoint.

    Implements depth_map.DepthProvider. `units` is declared rather than assumed:
    everything in depth_map is the geometry of a RELATIVE INVERSE depth map, and
    a metric map -- a phone's LiDAR, say -- needs different and simpler
    arithmetic. Declaring it means a future metric provider cannot quietly flow
    through the wrong maths; it will be refused until that path is built.
    """

    units = "relative_inverse"

    def __init__(self, dialect: str = "", api_key: str = "", endpoint: str = "",
                 version: str = "", image_field: str = "image",
                 extra_input: dict | None = None, output_field: str = "",
                 timeout_s: float = 25.0, transport=None):
        self.dialect = dialect
        self.api_key = api_key
        self.endpoint = endpoint
        self.version = version
        self.image_field = image_field or "image"
        self.extra_input = extra_input or {}
        self.output_field = output_field
        self.timeout_s = float(timeout_s or 25.0)
        self._transport = transport      # tests inject one; production has none

    @property
    def name(self) -> str:
        return f"hosted:{self.dialect}" if self.dialect else "none"

    def available(self) -> bool:
        """Config only -- never a network call.

        Checked once per scan, on the request path. Reaching out to find out
        would put a round trip in front of every photograph in order to learn
        something that cannot change between them.
        """
        if self.dialect == "replicate":
            return bool(self.api_key and self.version)
        if self.dialect == "http":
            return bool(self.endpoint)
        return False

    def _client(self, auth: bool) -> httpx.Client:
        headers = {}
        if auth and self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        kw = dict(timeout=httpx.Timeout(self.timeout_s, connect=8.0), headers=headers)
        if self._transport is not None:
            kw["transport"] = self._transport
        return httpx.Client(**kw)

    def depth(self, rgb: np.ndarray) -> np.ndarray | None:
        """Depth for this photograph, or None. None is a supported answer."""
        if not self.available() or rgb is None or getattr(rgb, "ndim", 0) != 3:
            return None
        shape = (rgb.shape[0], rgb.shape[1])
        started = time.monotonic()
        try:
            image = encode_image(rgb)
            if not image:
                return None
            if self.dialect == "replicate":
                raw = self._replicate(image)
            else:
                raw = self._http(image)
            if raw is None:
                return None
            arr = decode_depth(raw, shape)
            log.info("depth_provider_ok", provider=self.name,
                     ms=int((time.monotonic() - started) * 1000),
                     measured=arr is not None)
            return arr
        except Exception as exc:  # noqa: BLE001
            # Never raises. A model that fails costs a measurement, not a scan.
            log.warning("depth_provider_failed", provider=self.name,
                        error=str(exc)[:200],
                        ms=int((time.monotonic() - started) * 1000))
            return None

    # -- dialects ------------------------------------------------------------

    def _replicate(self, image: bytes) -> bytes | None:
        body = {
            "version": self.version,
            "input": {self.image_field: data_uri(image), **self.extra_input},
        }
        deadline = time.monotonic() + self.timeout_s
        with self._client(auth=True) as client:
            # "Prefer: wait" asks the API to hold the connection open until the
            # prediction finishes, which turns the usual create-then-poll dance
            # into one round trip for a model that runs in about two seconds.
            resp = client.post(REPLICATE_URL, json=body, headers={"Prefer": "wait"})
            if resp.status_code >= 400:
                log.warning("depth_replicate_http", status=resp.status_code,
                            body=resp.text[:200])
                return None
            payload = resp.json()

            # It may still have been queued. Poll rather than give up, but only
            # until the deadline the caller is already waiting against.
            while payload.get("status") not in REPLICATE_DONE:
                if time.monotonic() >= deadline:
                    log.warning("depth_replicate_timeout", status=payload.get("status"))
                    return None
                nxt = (payload.get("urls") or {}).get("get")
                if not nxt:
                    return None
                time.sleep(0.5)
                got = client.get(nxt)
                if got.status_code >= 400:
                    return None
                payload = got.json()

            if payload.get("status") != "succeeded":
                log.warning("depth_replicate_failed", status=payload.get("status"),
                            error=str(payload.get("error"))[:200])
                return None
            return self._fetch_output(payload.get("output"))

    def _http(self, image: bytes) -> bytes | None:
        """A plain endpoint: the photograph in, the depth map out.

        Covers a self-hosted container, a managed inference endpoint, or
        anything else that speaks HTTP -- which is what keeps the vendor a
        setting rather than a rewrite.
        """
        with self._client(auth=True) as client:
            resp = client.post(self.endpoint, content=image,
                               headers={"Content-Type": "image/jpeg"})
            if resp.status_code >= 400:
                log.warning("depth_http_status", status=resp.status_code,
                            body=resp.text[:200])
                return None
            ctype = resp.headers.get("content-type", "")
            if "json" not in ctype:
                return resp.content
            return self._fetch_output(self._from_json(resp.json()))

    def _from_json(self, payload):
        if isinstance(payload, dict):
            if self.output_field:
                return payload.get(self.output_field)
            for key in ("depth", "depth_map", "output", "image"):
                if key in payload:
                    return payload[key]
        return payload

    def _fetch_output(self, output) -> bytes | None:
        """Whatever the model called its answer, as bytes.

        Endpoints return one of three things: a URL to the file, a data URI, or
        bare base64. All three appear in the wild; all three are handled, and
        anything else is refused rather than guessed at.
        """
        if isinstance(output, (list, tuple)):
            output = output[0] if output else None
        if isinstance(output, dict):
            output = self._from_json(output)
        if not isinstance(output, str) or not output:
            log.warning("depth_output_unrecognised", kind=type(output).__name__)
            return None
        if output.startswith("data:"):
            _, _, b64 = output.partition(",")
            return _b64(b64)
        if output.startswith("http://") or output.startswith("https://"):
            # A fresh client with NO Authorization header: the file is served
            # from object storage, and sending the model key to a CDN is how a
            # credential ends up somewhere it was never meant to be.
            with self._client(auth=False) as client:
                got = client.get(output)
                if got.status_code >= 400:
                    log.warning("depth_output_fetch", status=got.status_code)
                    return None
                return got.content
        return _b64(output)


def _b64(text: str) -> bytes | None:
    try:
        return base64.b64decode(text, validate=False)
    except Exception:  # noqa: BLE001
        log.warning("depth_output_not_base64")
        return None


def from_settings(transport=None) -> DepthProvider:
    """The configured provider, or NullDepth when none is configured.

    Unconfigured is the default and behaves exactly as the app does today:
    no heights measured, every portion on its prior.

    The return type is DepthProvider on purpose. That Protocol described the
    contract every depth provider here implements and was referenced by
    nothing at all -- a written-down interface nothing checks drifts away from
    the code it describes, quietly, which is how this project keeps producing
    things that were built and never connected. Naming it here makes it a
    claim a type checker can fail.
    """
    from . import depth_map

    dialect = (settings.depth_provider or "").strip()
    if not dialect:
        return depth_map.NullDepth()

    # THE MODEL SIZE IS REQUIRED, AND ONLY "Small" IS ACCEPTED.
    #
    # This is a licence, not a tuning knob. Depth Anything V2 Small is Apache-2.0;
    # Base, Large and Giant are CC-BY-NC-4.0 and cannot serve an app that takes
    # subscriptions. The Replicate model this was first pointed at
    # (chenxwh/depth-anything-v2) DEFAULTS `model_size` to "Large", so a config
    # that simply omits it would ship non-commercial weights and every quality
    # number measured on it would set an expectation the shipping model cannot
    # meet. So omitting it switches depth OFF rather than falling to a default.
    size = (settings.depth_model_size or "").strip()
    if size.lower() != LICENSED_MODEL_SIZE.lower():
        log.warning("depth_model_size_not_licensed", declared=size or "(unset)",
                    required=LICENSED_MODEL_SIZE)
        return depth_map.NullDepth()

    extra = {}
    if settings.depth_model_input:
        try:
            parsed = json.loads(settings.depth_model_input)
            if isinstance(parsed, dict):
                extra = parsed
            else:
                log.warning("depth_model_input_not_an_object")
        except json.JSONDecodeError:
            log.warning("depth_model_input_not_json")

    # A size smuggled in through the free-form input would override the declared
    # one on the wire. Refused, not merged.
    stated = extra.get("model_size")
    if stated is not None and str(stated).strip().lower() != LICENSED_MODEL_SIZE.lower():
        log.warning("depth_model_input_size_conflict", model_size=str(stated)[:20],
                    required=LICENSED_MODEL_SIZE)
        return depth_map.NullDepth()
    if dialect == "replicate":
        # Sent explicitly on every prediction, so the endpoint's own default never
        # decides which weights run. An "http" endpoint is self-hosted and takes no
        # input fields; there the setting is the operator's declaration.
        extra["model_size"] = LICENSED_MODEL_SIZE

    provider = HostedDepth(
        dialect=dialect,
        api_key=settings.depth_api_key,
        endpoint=settings.depth_endpoint,
        version=settings.depth_model_version,
        image_field=settings.depth_image_field,
        extra_input=extra,
        output_field=settings.depth_output_field,
        timeout_s=settings.depth_timeout_s,
        transport=transport,
    )
    if not provider.available():
        log.warning("depth_provider_misconfigured", dialect=dialect)
        return depth_map.NullDepth()
    return provider
