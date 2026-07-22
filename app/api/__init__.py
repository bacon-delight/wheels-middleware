"""HTTP API routers."""

from fastapi import APIRouter

from . import documents, engagements, submissions

api_router = APIRouter()
api_router.include_router(engagements.router)
api_router.include_router(documents.router)
api_router.include_router(submissions.router)

__all__ = ["api_router"]
