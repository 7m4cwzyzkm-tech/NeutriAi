"""Model access layer.

The hybrid split, and why:

* **GPT-4o Vision** does *recognition*. It is the cheaper, faster call and it is
  good at "what objects are in this picture, where, and how much of the frame do
  they occupy". We ask it for geometry, not nutrition.
* **Claude** does *reasoning*. Given the detections plus the user's profile and
  history, it resolves ambiguity ("is that rice or couscous?"), sanity-checks
  portion sizes against real-world priors, and writes the human-facing advice.

  Note the boundary: context can help NAME a food, never decide whether it is
  there. Reasoning about which foods belong together led it to delete a third
  of a weighed test meal for "cuisine coherence". The photograph is the
  evidence; the model's expectations are not.

Each call is wrapped so that a provider outage degrades instead of 500ing:
``ask_vision`` and ``ask_reasoning`` both return ``None`` on failure and the
callers have deterministic fallbacks.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass, field
from typing import Any

import structlog
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential_jitter

from ...config import settings

log = structlog.get_logger()

# Rough public per-1M-token pricing, used only for budget telemetry.
_PRICES: dict[str, tuple[float, float]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


@dataclass(slots=True)
class AiCall:
    pipeline: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    ok: bool = True
    error: str | None = None
    payload: Any = None
    meta: dict = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        pin, pout = _PRICES.get(self.model, (0.0, 0.0))
        return (self.input_tokens * pin + self.output_tokens * pout) / 1_000_000


class TransientAiError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# JSON coercion. LLMs wrap JSON in prose or fences more often than we'd like.
# --------------------------------------------------------------------------
_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.S)


def coerce_json(text: str) -> Any:
    if not text:
        raise ValueError("empty model output")
    text = text.strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost balanced object/array.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = text.find(opener)
        end = text.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"model did not return JSON: {text[:200]}")


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------
def _openai():
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=settings.openai_api_key, timeout=settings.ai_timeout_s)


def _anthropic():
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=settings.anthropic_api_key, timeout=settings.ai_timeout_s)


@retry(
    retry=retry_if_exception_type(TransientAiError),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.6, max=6),
    reraise=True,
)
async def _openai_vision(
    system: str, user_text: str, images: list[str], model: str, max_tokens: int
) -> tuple[str, int, int]:
    content: list[dict[str, Any]] = [{"type": "text", "text": user_text}]
    for b64 in images:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "high"},
            }
        )
    try:
        resp = await _openai().chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            temperature=0.1,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
        )
    except Exception as exc:  # noqa: BLE001
        if any(s in str(exc).lower() for s in ("rate limit", "timeout", "overloaded", "503")):
            raise TransientAiError(str(exc)) from exc
        raise
    usage = resp.usage
    return (
        resp.choices[0].message.content or "",
        getattr(usage, "prompt_tokens", 0) or 0,
        getattr(usage, "completion_tokens", 0) or 0,
    )


@retry(
    retry=retry_if_exception_type(TransientAiError),
    stop=stop_after_attempt(3),
    wait=wait_exponential_jitter(initial=0.6, max=6),
    reraise=True,
)
async def _claude(
    system: str, user_text: str, images: list[str], model: str, max_tokens: int
) -> tuple[str, int, int]:
    content: list[dict[str, Any]] = []
    for b64 in images:
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/jpeg", "data": b64},
            }
        )
    content.append({"type": "text", "text": user_text})
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": content}],
    }
    # temperature is not accepted by every model/SDK combination. Where it is
    # rejected the SDK raises TypeError before any request is sent, and the
    # whole reasoning pass fails -- silently, because the caller is built to
    # degrade rather than 500. That meant every scan quietly ran without the
    # reasoning step while still reporting a result, which is the worst kind of
    # failure: invisible. Ask for it, drop it if it is refused.
    attempt = dict(kwargs, temperature=0.2)
    try:
        resp = await _anthropic().messages.create(**attempt)
    except TypeError as exc:
        if "temperature" not in str(exc):
            raise
        log.info("temperature_unsupported", model=model)
        resp = await _anthropic().messages.create(**kwargs)
    except Exception as exc:  # noqa: BLE001
        if any(s in str(exc).lower() for s in ("rate limit", "timeout", "overloaded", "529")):
            raise TransientAiError(str(exc)) from exc
        raise
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    return text, resp.usage.input_tokens, resp.usage.output_tokens


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
async def ask_vision(
    *,
    pipeline: str,
    system: str,
    user_text: str,
    images: list[str],
    model: str | None = None,
    max_tokens: int = 2000,
) -> AiCall:
    model = model or settings.vision_model
    call = AiCall(pipeline=pipeline, model=model)
    t0 = time.perf_counter()
    try:
        raw, tin, tout = await _openai_vision(system, user_text, images, model, max_tokens)
        call.input_tokens, call.output_tokens = tin, tout
        call.payload = coerce_json(raw)
    except Exception as exc:  # noqa: BLE001
        call.ok, call.error = False, str(exc)[:400]
        log.warning("vision_failed", pipeline=pipeline, model=model, error=call.error)
    call.latency_ms = int((time.perf_counter() - t0) * 1000)
    return call


async def ask_reasoning(
    *,
    pipeline: str,
    system: str,
    user_text: str,
    images: list[str] | None = None,
    model: str | None = None,
    max_tokens: int = 3000,
) -> AiCall:
    model = model or settings.reasoning_model
    call = AiCall(pipeline=pipeline, model=model)
    t0 = time.perf_counter()
    try:
        raw, tin, tout = await _claude(system, user_text, images or [], model, max_tokens)
        call.input_tokens, call.output_tokens = tin, tout
        call.payload = coerce_json(raw)
    except Exception as exc:  # noqa: BLE001
        call.ok, call.error = False, str(exc)[:400]
        log.warning("reasoning_failed", pipeline=pipeline, model=model, error=call.error)
    call.latency_ms = int((time.perf_counter() - t0) * 1000)
    return call


async def ask_text(
    *, pipeline: str, system: str, user_text: str, model: str | None = None, max_tokens: int = 600
) -> AiCall:
    """Free-form (non-JSON) generation — used by the motivation engine."""
    model = model or settings.motivation_model
    call = AiCall(pipeline=pipeline, model=model)
    t0 = time.perf_counter()
    try:
        raw, tin, tout = await _claude(system, user_text, [], model, max_tokens)
        call.input_tokens, call.output_tokens = tin, tout
        call.payload = raw.strip()
    except Exception as exc:  # noqa: BLE001
        call.ok, call.error = False, str(exc)[:400]
    call.latency_ms = int((time.perf_counter() - t0) * 1000)
    return call


async def record_usage(call: AiCall, user_id: str | None) -> None:
    """Fire-and-forget cost telemetry. Never blocks or breaks the request."""
    try:
        from ...db import service

        service().table("ai_usage").insert(
            {
                "user_id": user_id,
                "pipeline": call.pipeline,
                "model": call.model,
                "input_tokens": call.input_tokens,
                "output_tokens": call.output_tokens,
                "cost_usd": round(call.cost_usd, 6),
                "latency_ms": call.latency_ms,
                "ok": call.ok,
            }
        ).execute()
    except Exception:  # noqa: BLE001
        log.debug("ai_usage_write_failed", pipeline=call.pipeline)


