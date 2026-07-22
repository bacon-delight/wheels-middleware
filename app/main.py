"""FastAPI application + Lambda entrypoint.

Packaged as a container image (ECR) and run on Lambda via Mangum. Infra owns the Lambda
resource, alias, and provisioned concurrency; this repo's CI only pushes a new image and
repoints the function/alias — no infra redeploy. Full API routers land in Phase 2; this is
the deployable skeleton with health + CORS.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from mangum import Mangum

from .config import get_settings

logging.getLogger().setLevel(logging.INFO)
settings = get_settings()

app = FastAPI(title="Wheels Contract Intelligence API", version="0.1.0")

ALLOWED_ORIGINS = [
    "https://wheels.logiforma.dev",
    "http://localhost:5173",
    "http://localhost:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "wheels-middleware",
        "region": settings.core_region,
        "llm_provider": settings.llm_provider,
    }


@app.get("/")
def root() -> dict:
    return {"service": "Wheels Contract Intelligence API", "docs": "/docs"}


handler = Mangum(app)
