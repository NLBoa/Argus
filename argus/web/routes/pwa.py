"""PWA: manifest and service worker (offline: last-loaded pages and all static assets)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response

from argus import __version__
from argus.web.deps import Assets
from argus.web.routes.common import templates

router = APIRouter()
PRECACHE = [
    "dist/app.css",
    "app.js",
    "vendor/alpine.min.js",
    "vendor/htmx.min.js",
    "icons/sprite.svg",
    "icons/argus.svg",
    "fonts/ahn-latin.woff2",
]


@router.get("/manifest.webmanifest")
def manifest() -> JSONResponse:
    return JSONResponse(
        {
            "name": "Argus",
            "short_name": "Argus",
            "description": "Your courses, deadlines, and study plan.",
            "start_url": "/today",
            "scope": "/",
            "display": "standalone",
            "background_color": "#eef2f5",
            "theme_color": "#0b636b",
            "icons": [
                {"src": "/static/icons/argus.svg", "sizes": "any", "type": "image/svg+xml"},
                {"src": "/static/icons/argus-192.png", "sizes": "192x192", "type": "image/png"},
                {
                    "src": "/static/icons/argus-512.png",
                    "sizes": "512x512",
                    "type": "image/png",
                    "purpose": "any maskable",
                },
            ],
        },
        media_type="application/manifest+json",
    )


@router.get("/sw.js")
def service_worker(request: Request) -> Response:
    asset = templates(request).env.globals["asset"]
    assert isinstance(asset, Assets)
    urls = [asset(p) for p in PRECACHE] + ["/offline"]
    body = (
        templates(request)
        .get_template("sw.js")
        .render(version=__version__ + "-" + str(abs(hash(tuple(urls))) % 10**8), urls=urls)
    )
    return Response(
        body,
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


@router.get("/offline", response_class=HTMLResponse)
def offline(request: Request) -> HTMLResponse:
    return templates(request).TemplateResponse(request, "pages/offline.html", {})
