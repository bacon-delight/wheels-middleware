"""HTTP API routers."""

from fastapi import APIRouter

from . import (
    customers,
    documents,
    engagements,
    finance,
    insights,
    me,
    payments,
    submissions,
    users,
    vehicles,
)

api_router = APIRouter()
api_router.include_router(engagements.router)
api_router.include_router(customers.router)
api_router.include_router(vehicles.router)
api_router.include_router(documents.router)
api_router.include_router(submissions.router)
api_router.include_router(me.router)
api_router.include_router(finance.router)
api_router.include_router(users.router)
api_router.include_router(payments.router)
api_router.include_router(insights.router)

__all__ = ["api_router"]
