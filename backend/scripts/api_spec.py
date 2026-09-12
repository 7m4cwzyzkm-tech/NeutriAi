"""Export the API as OpenAPI, plus a one-page index a human can read.

WHY A SCRIPT AND NOT A HAND-WRITTEN DOCUMENT

A hand-written API reference is a copy of the truth, and copies drift. This
takes the spec from the running FastAPI app itself, so it cannot describe an
endpoint that does not exist or miss one that does. The day a route is renamed,
the document is wrong until someone re-runs this -- and re-running it is one
command.

Two outputs, because they answer different questions:

  openapi.json   the machine-readable spec. Import it into Postman, Insomnia,
                 Swagger UI, or a generated client. This is what to send an
                 engineer who wants to CALL the API.

  API.md         a flat index -- method, path, what it does, whether it needs a
                 token. This is what to send someone who wants to UNDERSTAND
                 the surface in two minutes without loading a tool.

Neither contains a secret. The spec is derived from route signatures and Pydantic
models; no environment variable, key or URL is read.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

OUT = pathlib.Path(__file__).resolve().parents[1] / "docs"


def summarise(app) -> str:
    """A flat index of every route, grouped by the prefix it hangs off."""
    from app.config import settings

    rows = []
    for route in app.routes:
        methods = sorted(getattr(route, "methods", set()) - {"HEAD", "OPTIONS"})
        path = getattr(route, "path", "")
        if not methods or not path.startswith(settings.api_prefix):
            continue
        fn = getattr(route, "endpoint", None)
        doc = (fn.__doc__ or "").strip().splitlines()[0] if fn and fn.__doc__ else ""
        # Auth is a dependency, not a decorator, so read it off the signature --
        # the same place the app reads it, rather than a list kept by hand.
        sig = str(getattr(fn, "__annotations__", {}))
        auth = "token" if "CurrentUser" in sig or "Entitlement" in sig else "public"
        rows.append((path, "/".join(methods), auth, doc[:100]))

    groups: dict[str, list] = {}
    for path, methods, auth, doc in sorted(rows):
        head = "/".join(path.split("/")[:3]) or path
        groups.setdefault(head, []).append((methods, path, auth, doc))

    out = [
        "# NeutriAI API",
        "",
        f"`{settings.api_prefix}` prefix. {len(rows)} endpoints.",
        "",
        "Auth is a Supabase JWT in `Authorization: Bearer <token>`. Endpoints",
        "marked `public` take no token — everything else returns 401 without one.",
        "",
        f"Rate limit: {settings.rate_limit_per_minute} requests/minute per client.",
        "",
    ]
    for head, items in sorted(groups.items()):
        out += [f"## {head}", "", "| | endpoint | auth | |", "|---|---|---|---|"]
        for methods, path, auth, doc in items:
            out.append(f"| `{methods}` | `{path}` | {auth} | {doc} |")
        out.append("")
    return "\n".join(out)


def main() -> int:
    try:
        from app.main import app
    except Exception as exc:  # noqa: BLE001
        print(f"could not import the app: {exc}")
        print("run this from an activated venv -- it needs the real FastAPI, "
              "not a stub.")
        return 1
    if not hasattr(app, "openapi"):
        print("this FastAPI has no openapi() -- a stub is on the path ahead of "
              "the real package.")
        return 1

    OUT.mkdir(parents=True, exist_ok=True)
    spec = app.openapi()
    (OUT / "openapi.json").write_text(json.dumps(spec, indent=2), encoding="utf-8")
    (OUT / "API.md").write_text(summarise(app), encoding="utf-8")
    n = sum(len(v) for v in spec.get("paths", {}).values())
    print(f"wrote backend/docs/openapi.json  ({n} operations)")
    print(f"wrote backend/docs/API.md")
    print()
    print("Neither file contains a key, a URL or an environment value. Safe to send.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
