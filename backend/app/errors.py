"""One error shape for the whole API, so the mobile client parses one thing."""
from __future__ import annotations

from typing import Any

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = structlog.get_logger()


class AppError(Exception):
    """Base for every error we raise on purpose."""

    status_code = status.HTTP_400_BAD_REQUEST
    code = "bad_request"

    def __init__(self, message: str, *, detail: Any = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail
        if code:
            self.code = code


class NotFound(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "not_found"


class Unauthorized(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"


class Forbidden(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"


class PaymentRequired(AppError):
    """Raised when a Pro-only route is hit without a live entitlement."""

    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "subscription_required"


class QuotaExceeded(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "quota_exceeded"


class UpstreamError(AppError):
    """A third party (OpenAI, USDA, Stripe, Fitbit) failed us."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "upstream_error"


def _body(code: str, message: str, detail: Any = None, request_id: str | None = None) -> dict:
    out: dict[str, Any] = {"error": {"code": code, "message": message}}
    if detail is not None:
        out["error"]["detail"] = detail
    if request_id:
        out["error"]["request_id"] = request_id
    return out


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(request: Request, exc: AppError):
        rid = getattr(request.state, "request_id", None)
        log.warning("app_error", code=exc.code, message=exc.message, path=request.url.path, rid=rid)
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(exc.code, exc.message, exc.detail, rid),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        rid = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=422,
            content=_body("validation_error", "Request body failed validation.", exc.errors(), rid),
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        rid = getattr(request.state, "request_id", None)
        return JSONResponse(
            status_code=exc.status_code,
            content=_body(f"http_{exc.status_code}", str(exc.detail), None, rid),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", None)
        log.exception("unhandled", path=request.url.path, rid=rid)
        # Never leak a stack trace to a phone.
        return JSONResponse(
            status_code=500,
            content=_body("internal_error", "Something went wrong on our side.", None, rid),
        )
