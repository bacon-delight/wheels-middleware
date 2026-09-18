"""Vehicle inventory: what Wheels owns, what is out on lease, and what belongs to a customer.

Inventory is org-level, so the listing routes use `require_provider_principal`. The one
exception is `/engagements/{engagement_id}/vehicles`, which a client may call for their own
units — the path parameter must keep that exact name because `membership_dep` binds by it.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..auth.deps import get_repo, membership_dep, require_provider_principal
from ..auth.principal import Principal
from ..billing.fleet import effective_fleet_size, fleet_source, is_locked
from ..store.models import (
    AuditEvent,
    BodyClass,
    DutyBand,
    LeaseStructure,
    Membership,
    Powertrain,
    Vehicle,
    VehicleOwnership,
    VehicleStatus,
)
from ..store.repository import Repository, new_id, utcnow

router = APIRouter(tags=["vehicles"])


class CreateVehicleIn(BaseModel):
    vin: str | None = None
    unit_number: str | None = None
    year: int | None = None
    make: str | None = None
    model: str | None = None
    body_class: str = BodyClass.SEDAN.value
    powertrain: str = Powertrain.ICE.value
    ownership: str = VehicleOwnership.WHEELS_OWNED.value
    status: str | None = None
    customer_id: str | None = None
    lease_structure: str | None = None
    lease_term_months: int | None = Field(default=None, ge=24, le=120)
    in_service_date: str | None = None
    odometer: int | None = None
    notes: str | None = None


class UpdateVehicleIn(BaseModel):
    vin: str | None = None
    unit_number: str | None = None
    year: int | None = None
    make: str | None = None
    model: str | None = None
    body_class: str | None = None
    powertrain: str | None = None
    status: str | None = None
    lease_structure: str | None = None
    lease_term_months: int | None = Field(default=None, ge=24, le=120)
    in_service_date: str | None = None
    odometer: int | None = None
    notes: str | None = None


class AssignIn(BaseModel):
    engagement_id: str


class BulkAssignIn(BaseModel):
    vehicle_ids: list[str] = Field(min_length=1, max_length=200)
    engagement_id: str


def _enum_or_400(value: str | None, enum_cls, label: str):
    if value is None:
        return None
    allowed = {e.value for e in enum_cls}
    if value not in allowed:
        raise HTTPException(400, f"{label} must be one of {sorted(allowed)}")
    return value


def _sync_engagement_fleet(repo: Repository, engagement_id: str) -> int:
    """Recompute the engagement's effective fleet from the inventory.

    Assignments still move the count once billing is locked, so the UI can surface drift, but
    `fleet_size` and the recurring dues freeze.
    """
    count = repo.count_vehicles_for_engagement(engagement_id)
    engagement = repo.get_engagement(engagement_id)
    if engagement is None:
        return count
    if is_locked(engagement.status):
        return count
    fleet = effective_fleet_size(count, engagement.fleet_size_override)
    monthly = engagement.monthly_recurring
    subs = repo.list_submissions(engagement_id)
    if subs:
        import json

        from ..billing.estimate import compute_monthly_recurring
        from ..objects import billing_config_key
        from ..store.s3 import S3Store

        try:
            raw = S3Store().get_bytes(billing_config_key(engagement_id, subs[0].submission_id))
            monthly = compute_monthly_recurring(json.loads(raw), fleet)
        except Exception:  # noqa: BLE001 - the config only exists once billing is set up
            pass
    repo.set_engagement_billing(engagement_id, fleet, monthly)
    return count


@router.post("/vehicles", status_code=201)
def create_vehicle(
    body: CreateVehicleIn,
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    _enum_or_400(body.body_class, BodyClass, "body_class")
    _enum_or_400(body.powertrain, Powertrain, "powertrain")
    _enum_or_400(body.ownership, VehicleOwnership, "ownership")
    _enum_or_400(body.status, VehicleStatus, "status")
    _enum_or_400(body.lease_structure, LeaseStructure, "lease_structure")
    customer_owned = body.ownership == VehicleOwnership.CUSTOMER_OWNED.value
    if customer_owned:
        if not body.customer_id:
            raise HTTPException(400, "customer_id is required for a customer-owned vehicle")
        if repo.get_customer(body.customer_id) is None:
            raise HTTPException(404, "customer not found")
    status = body.status or (
        VehicleStatus.ON_ORDER.value if customer_owned else VehicleStatus.IN_STOCK.value
    )
    try:
        vehicle = Vehicle(
            vehicle_id=new_id(),
            **body.model_dump(exclude={"status"}),
            status=status,
            created_by=principal.user_id,
            created_at=utcnow(),
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"vehicle": repo.put_vehicle(vehicle)}


@router.get("/vehicles")
def list_vehicles(
    ownership: str | None = Query(default=None),
    status: str | None = Query(default=None),
    duty_band: str | None = Query(default=None),
    engagement_id: str | None = Query(default=None),
    customer_id: str | None = Query(default=None),
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    _enum_or_400(ownership, VehicleOwnership, "ownership")
    _enum_or_400(status, VehicleStatus, "status")
    _enum_or_400(duty_band, DutyBand, "duty_band")
    if engagement_id:
        vehicles = repo.list_vehicles_by_engagement(engagement_id)
    elif customer_id:
        vehicles = [
            v
            for e in repo.list_customer_engagements(customer_id)
            for v in repo.list_vehicles_by_engagement(e.engagement_id)
        ]
        vehicles += [
            v
            for v in repo.list_vehicles_by_ownership(VehicleOwnership.CUSTOMER_OWNED.value)
            if v.customer_id == customer_id and not v.engagement_id
        ]
    else:
        owners = [ownership] if ownership else [o.value for o in VehicleOwnership]
        vehicles = [
            v for o in owners for v in repo.list_vehicles_by_ownership(o, status, duty_band)
        ]
    if status:
        vehicles = [v for v in vehicles if v.status == status]
    if duty_band:
        vehicles = [v for v in vehicles if v.duty_band == duty_band]
    if ownership:
        vehicles = [v for v in vehicles if v.ownership == ownership]
    vehicles.sort(key=lambda v: (v.unit_number or v.vin or v.vehicle_id))
    return {"vehicles": vehicles, "count": len(vehicles)}


@router.get("/vehicles/summary")
def vehicles_summary(
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    """Inventory tiles. Counts come from GSI1 queries, never a scan."""
    by_status: dict[str, int] = {}
    by_ownership: dict[str, int] = {}
    for o in VehicleOwnership:
        owned_total = 0
        for st in VehicleStatus:
            n = repo.vehicle_status_count(o.value, st.value)
            if n:
                by_status[st.value] = by_status.get(st.value, 0) + n
                owned_total += n
        by_ownership[o.value] = owned_total
    available = repo.vehicle_status_count(
        VehicleOwnership.WHEELS_OWNED.value, VehicleStatus.IN_STOCK.value
    )
    return {
        "total": sum(by_ownership.values()),
        "available_to_lease": available,
        "assigned": by_status.get(VehicleStatus.ASSIGNED.value, 0),
        "customer_owned": by_ownership.get(VehicleOwnership.CUSTOMER_OWNED.value, 0),
        "by_status": by_status,
        "by_ownership": by_ownership,
    }


@router.get("/vehicles/{vehicle_id}")
def get_vehicle(
    vehicle_id: str,
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    vehicle = repo.get_vehicle(vehicle_id)
    if vehicle is None:
        raise HTTPException(404, "vehicle not found")
    return {"vehicle": vehicle, "assignments": repo.list_vehicle_assignments(vehicle_id)}


@router.patch("/vehicles/{vehicle_id}")
def update_vehicle(
    vehicle_id: str,
    body: UpdateVehicleIn,
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    if repo.get_vehicle(vehicle_id) is None:
        raise HTTPException(404, "vehicle not found")
    _enum_or_400(body.body_class, BodyClass, "body_class")
    _enum_or_400(body.powertrain, Powertrain, "powertrain")
    _enum_or_400(body.status, VehicleStatus, "status")
    _enum_or_400(body.lease_structure, LeaseStructure, "lease_structure")
    try:
        vehicle = repo.update_vehicle(vehicle_id, body.model_dump(exclude_none=True))
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"vehicle": vehicle}


def _assign_one(repo: Repository, vehicle_id: str, engagement, actor: Principal) -> Vehicle:
    vehicle = repo.get_vehicle(vehicle_id)
    if vehicle is None:
        raise HTTPException(404, f"vehicle {vehicle_id} not found")
    if vehicle.engagement_id and vehicle.engagement_id != engagement.engagement_id:
        raise HTTPException(409, f"vehicle {vehicle_id} is already assigned")
    if vehicle.status == VehicleStatus.RETIRED.value:
        raise HTTPException(409, f"vehicle {vehicle_id} is retired")
    if (
        vehicle.ownership == VehicleOwnership.CUSTOMER_OWNED.value
        and vehicle.customer_id
        and engagement.customer_id
        and vehicle.customer_id != engagement.customer_id
    ):
        raise HTTPException(400, f"vehicle {vehicle_id} belongs to a different customer")
    assigned = repo.assign_vehicle(
        vehicle_id, engagement.engagement_id, engagement.customer_id, actor.user_id
    )
    if assigned is None:
        raise HTTPException(404, f"vehicle {vehicle_id} not found")
    return assigned


@router.post("/vehicles/{vehicle_id}:assign")
def assign_vehicle(
    vehicle_id: str,
    body: AssignIn,
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    engagement = repo.get_engagement(body.engagement_id)
    if engagement is None:
        raise HTTPException(404, "engagement not found")
    vehicle = _assign_one(repo, vehicle_id, engagement, principal)
    count = _sync_engagement_fleet(repo, engagement.engagement_id)
    repo.put_audit(
        AuditEvent(
            engagement_id=engagement.engagement_id, event_id=new_id(), ts=utcnow(),
            actor_id=principal.user_id, actor_role=principal.group_role.value,
            actor_name=principal.name, action="vehicle_assigned", target=vehicle_id,
        )
    )
    return {"vehicle": vehicle, "assigned_vehicle_count": count}


@router.post("/vehicles:bulk-assign")
def bulk_assign(
    body: BulkAssignIn,
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    """A real engagement takes delivery of dozens of units at once."""
    engagement = repo.get_engagement(body.engagement_id)
    if engagement is None:
        raise HTTPException(404, "engagement not found")
    assigned, failed = [], []
    for vid in body.vehicle_ids:
        try:
            assigned.append(_assign_one(repo, vid, engagement, principal))
        except HTTPException as e:
            failed.append({"vehicle_id": vid, "detail": e.detail})
    count = _sync_engagement_fleet(repo, engagement.engagement_id)
    if assigned:
        repo.put_audit(
            AuditEvent(
                engagement_id=engagement.engagement_id, event_id=new_id(), ts=utcnow(),
                actor_id=principal.user_id, actor_role=principal.group_role.value,
                actor_name=principal.name, action="vehicle_assigned",
                target=f"{len(assigned)} vehicles",
            )
        )
    return {"assigned": assigned, "failed": failed, "assigned_vehicle_count": count}


@router.post("/vehicles/{vehicle_id}:release")
def release_vehicle(
    vehicle_id: str,
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    vehicle = repo.get_vehicle(vehicle_id)
    if vehicle is None:
        raise HTTPException(404, "vehicle not found")
    previous = vehicle.engagement_id
    released = repo.release_vehicle(vehicle_id)
    count = None
    if previous:
        count = _sync_engagement_fleet(repo, previous)
        repo.put_audit(
            AuditEvent(
                engagement_id=previous, event_id=new_id(), ts=utcnow(),
                actor_id=principal.user_id, actor_role=principal.group_role.value,
                actor_name=principal.name, action="vehicle_released", target=vehicle_id,
            )
        )
    return {"vehicle": released, "assigned_vehicle_count": count}


@router.get("/engagements/{engagement_id}/vehicles")
def list_engagement_vehicles(
    engagement_id: str,
    member: Membership = Depends(membership_dep),
    repo: Repository = Depends(get_repo),
):
    """The one vehicle route a client may call — their own engagement's units."""
    vehicles = repo.list_vehicles_by_engagement(engagement_id)
    engagement = repo.get_engagement(engagement_id)
    override = engagement.fleet_size_override if engagement else None
    return {
        "vehicles": sorted(vehicles, key=lambda v: (v.unit_number or v.vehicle_id)),
        "assigned_vehicle_count": len(vehicles),
        "fleet_size": engagement.fleet_size if engagement else len(vehicles),
        "fleet_size_override": override,
        "fleet_size_source": fleet_source(override),
    }
