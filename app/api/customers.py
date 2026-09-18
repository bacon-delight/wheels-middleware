"""Customers: the parent of engagements.

One customer holds many engagements — a fleet client that leases 40 vehicles in March and 20
more in September is one customer with two engagements. Customers are org-level (any Wheels
provider sees all of them), so these routes use `require_provider_principal` rather than the
engagement-scoped `require_provider`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth.deps import get_principal, get_repo, require_provider_principal
from ..auth.principal import Principal
from ..store.models import Customer
from ..store.repository import Repository, new_id, utcnow

router = APIRouter(tags=["customers"])


class CreateCustomerIn(BaseModel):
    legal_name: str
    display_name: str | None = None
    industry: str | None = None
    billing_address: str | None = None
    city: str | None = None
    country: str | None = None
    primary_contact_email: str | None = None
    primary_contact_phone: str | None = None
    notes: str | None = None


class UpdateCustomerIn(BaseModel):
    legal_name: str | None = None
    display_name: str | None = None
    industry: str | None = None
    billing_address: str | None = None
    city: str | None = None
    country: str | None = None
    primary_contact_email: str | None = None
    primary_contact_phone: str | None = None
    status: str | None = None
    notes: str | None = None


@router.post("/customers", status_code=201)
def create_customer(
    body: CreateCustomerIn,
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    name = body.legal_name.strip()
    if not name:
        raise HTTPException(400, "legal_name is required")
    existing = next(
        (c for c in repo.list_customers() if c.legal_name.strip().lower() == name.lower()), None
    )
    if existing is not None:
        raise HTTPException(409, f"customer '{name}' already exists")
    customer = repo.put_customer(
        Customer(
            customer_id=new_id(),
            legal_name=name,
            **body.model_dump(exclude={"legal_name"}),
            created_by=principal.user_id,
            created_at=utcnow(),
        )
    )
    return {"customer": customer}


@router.get("/customers")
def list_customers(
    principal: Principal = Depends(get_principal),
    repo: Repository = Depends(get_repo),
):
    """Providers see every customer; a client sees only the ones they are engaged with."""
    customers = repo.list_customers()
    if not principal.is_provider:
        eids = set(repo.list_user_engagement_ids(principal.user_id))
        allowed = {
            e.customer_id
            for e in (repo.get_engagement(eid) for eid in eids)
            if e is not None and e.customer_id
        }
        customers = [c for c in customers if c.customer_id in allowed]
    rows = []
    for c in customers:
        engagements = repo.list_customer_engagements(c.customer_id)
        rows.append(
            {
                **c.model_dump(mode="json"),
                "engagement_count": len(engagements),
                "fleet_size": sum(e.fleet_size or 0 for e in engagements),
            }
        )
    rows.sort(key=lambda r: r["legal_name"].lower())
    return {"customers": rows}


@router.get("/customers/{customer_id}")
def get_customer(
    customer_id: str,
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    customer = repo.get_customer(customer_id)
    if customer is None:
        raise HTTPException(404, "customer not found")
    engagements = repo.list_customer_engagements(customer_id)
    contacts: list[dict] = []
    seen: set[str] = set()
    vehicles: list[dict] = []
    for e in engagements:
        for m in repo.list_members(e.engagement_id):
            if m.role.value == "client" and m.user_id not in seen:
                seen.add(m.user_id)
                contacts.append(
                    {**m.model_dump(mode="json"), "engagement_name": e.name}
                )
        vehicles.extend(
            v.model_dump(mode="json") for v in repo.list_vehicles_by_engagement(e.engagement_id)
        )
    return {
        "customer": customer,
        "engagements": sorted(engagements, key=lambda e: e.created_at, reverse=True),
        "contacts": contacts,
        "vehicles": vehicles,
        "vehicle_count": len(vehicles),
    }


@router.patch("/customers/{customer_id}")
def update_customer(
    customer_id: str,
    body: UpdateCustomerIn,
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    if repo.get_customer(customer_id) is None:
        raise HTTPException(404, "customer not found")
    patch = {k_: v for k_, v in body.model_dump(exclude_none=True).items()}
    customer = repo.update_customer(customer_id, patch)
    # `client_name` is a denormalized display copy on each engagement; keep it in step.
    if customer is not None and "legal_name" in patch:
        for e in repo.list_customer_engagements(customer_id):
            repo.set_engagement_customer(e.engagement_id, customer_id, customer.legal_name)
    return {"customer": customer}
