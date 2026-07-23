"""HTTP API routers."""

from fastapi import APIRouter

from . import documents, engagements, finance, me, submissions, users

api_router = APIRouter()
api_router.include_router(engagements.router)
api_router.include_router(documents.router)
api_router.include_router(submissions.router)
api_router.include_router(me.router)
api_router.include_router(finance.router)
api_router.include_router(users.router)

__all__ = ["api_router"]
