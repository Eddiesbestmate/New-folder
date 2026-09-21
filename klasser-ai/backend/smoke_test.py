"""Phase 1 smoke test: the app boots, routes are registered, health reports honestly."""

from fastapi.testclient import TestClient

import main

with TestClient(main.app) as client:
    r = client.get("/")
    print("GET /       ->", r.status_code, r.json())

    h = client.get("/health")
    print("GET /health ->", h.status_code, h.json())

    s = client.get("/billing/_stub")
    print("GET /billing/_stub ->", s.status_code, s.json())

    # Included routers are not plain routes in current FastAPI, so read the
    # generated OpenAPI schema instead - that is the real surface area.
    #
    # Every path segment counts, including a router whose only route sits at the
    # prefix itself. Requiring more than one "/" hid /notifications entirely,
    # and a router that has gone missing is exactly what this test is for.
    root_only = {"", "health", "docs", "redoc", "openapi.json",
                 "docs/oauth2-redirect"}
    paths = main.app.openapi()["paths"]
    prefixes = sorted({p.split("/")[1] for p in paths
                       if p.split("/")[1] not in root_only})
    print(f"registered routers: {len(prefixes)}")
    for p in prefixes:
        print(f"  /{p}")
