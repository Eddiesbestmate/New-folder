"""
Cross-check every apiCall() path in the frontend against the app's real routes.

Catches typos and stale paths that would only show up as a 404 in the browser.

    python check_frontend_calls.py
"""

import re
import sys
from pathlib import Path

import main

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"

# apiCall('GET', '/auth/me')  /  apiCall('PUT', `/schools/teachers/${id}`)
CALL = re.compile(r"""apiCall\(\s*['"](\w+)['"]\s*,\s*['"`]([^'"`]+)['"`]""")


def normalise(path: str) -> str:
    """Turn a JS template path into a FastAPI-style path for comparison."""
    path = path.split("?")[0]
    # ${...} -> {param}
    path = re.sub(r"\$\{[^}]+\}", "{param}", path)
    return path.rstrip("/") or "/"


def route_patterns() -> set[tuple[str, str]]:
    """
    Collect every (method, path) the app serves, from the OpenAPI schema.

    Not from app.routes: this FastAPI version stores included routers as opaque
    _IncludedRouter objects carrying neither .path nor .methods, so walking that
    list finds only the six top-level endpoints and reports every router
    endpoint as missing. The generated schema is the authoritative list.
    """
    schema = main.app.openapi()
    return {
        (method.upper(), re.sub(r"\{[^}]+\}", "{param}", path).rstrip("/") or "/")
        for path, operations in schema.get("paths", {}).items()
        for method in operations
    }


def main_check() -> int:
    routes = route_patterns()
    problems: list[str] = []
    dynamic: list[str] = []
    checked = 0

    for js in sorted(FRONTEND.rglob("*.js")) + sorted(FRONTEND.rglob("*.html")):
        text = js.read_text(encoding="utf-8")
        for method, raw in CALL.findall(text):
            checked += 1
            # A path that starts with an expression is assembled at runtime and
            # cannot be resolved here. Report it rather than calling it missing.
            if raw.lstrip().startswith("${"):
                dynamic.append(f"{js.name}: {method.upper()} {raw}")
                continue
            if (method.upper(), normalise(raw)) not in routes:
                problems.append(f"{js.name}: {method.upper()} {raw}  ->  no such route")

    print(f"Checked {checked} apiCall() sites against {len(routes)} routes\n")

    for d in dynamic:
        print(f"  dynamic  {d}")
    if dynamic:
        print(f"  ({len(dynamic)} built at runtime - not statically checkable)\n")

    if problems:
        for p in problems:
            print(f"  MISSING  {p}")
        print(f"\n{len(problems)} bad call site(s).")
        return 1

    print(f"All {checked - len(dynamic)} static call sites match a real route.")
    return 0


if __name__ == "__main__":
    sys.exit(main_check())
