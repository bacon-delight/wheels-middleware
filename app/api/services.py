"""The service catalog, and who uses what.

Org-level, like customers and users: the catalog is a property of Wheels' business rather than
of any one engagement, so a provider sees all of it and a customer sees none of it.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth.deps import get_repo, membership_dep, require_provider_principal
from ..auth.principal import Principal
from ..catalog.models import ServiceItem, ServiceProgram
from ..catalog.resolve import normalise, slugify
from ..catalog.store import invalidate, load_snapshot
from ..store.models import Membership
from ..store.repository import Repository, utcnow

router = APIRouter(tags=["services"])


class CreateProgramIn(BaseModel):
    name: str
    category: str | None = None
    description: str | None = None
    aliases: list[str] = []


class UpdateProgramIn(BaseModel):
    name: str | None = None
    category: str | None = None
    description: str | None = None
    status: str | None = None


class CreateItemIn(BaseModel):
    name: str
    default_frequency: str | None = None
    aliases: list[str] = []


class AliasIn(BaseModel):
    alias: str


class PromoteIn(BaseModel):
    name: str | None = None
    category: str | None = None


class LinkIn(BaseModel):
    program_id: str


@router.get("/services")
def list_services(
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    """The catalog with adoption counts.

    Adoption comes from one GSI2 query per program rather than a scan over every extracted
    term, which is what keeps this page cheap as the number of engagements grows.
    """
    snapshot = load_snapshot(repo, fresh=True)
    programs: list[dict[str, Any]] = []
    for program in snapshot.programs:
        engagements = repo.engagements_using(program.program_id)
        programs.append(
            {
                **program.model_dump(mode="json"),
                "item_count": len(snapshot.items_for(program.program_id)),
                "engagement_count": len(engagements),
            }
        )
    programs.sort(key=lambda p: (-p["engagement_count"], -p["contract_count"], p["name"]))
    return {
        "programs": programs,
        "totals": {
            "programs": len(snapshot.active_programs),
            "items": len(snapshot.items),
            "in_use": sum(1 for p in programs if p["engagement_count"]),
            "unmatched": len(repo.list_unmatched()),
        },
        "categories": sorted({p.category for p in snapshot.programs if p.category}),
    }


@router.get("/services/unmatched")
def list_unmatched(
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    """Program names extraction found that the catalog does not know.

    Deliberately a queue and not an auto-create. Every promoted name changes the denominator
    every coverage percentage is measured against, so it is a decision a person makes.
    """
    return {"unmatched": [u.model_dump(mode="json") for u in repo.list_unmatched()]}


@router.get("/services/{program_id}")
def get_service(
    program_id: str,
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    snapshot = load_snapshot(repo, fresh=True)
    program = next((p for p in snapshot.programs if p.program_id == program_id), None)
    if program is None:
        raise HTTPException(404, "service not found")
    engagement_ids = repo.engagements_using(program_id)
    engagements = []
    for eid in engagement_ids:
        e = repo.get_engagement(eid)
        if e is not None:
            engagements.append(
                {
                    "engagement_id": e.engagement_id,
                    "name": e.name,
                    "client_name": e.client_name,
                    "customer_id": e.customer_id,
                    "status": e.status,
                }
            )
    return {
        "program": program.model_dump(mode="json"),
        "items": [i.model_dump(mode="json") for i in snapshot.items_for(program_id)],
        "engagements": engagements,
        "customers": sorted({e["client_name"] for e in engagements}),
    }


@router.post("/services", status_code=201)
def create_service(
    body: CreateProgramIn,
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "a service needs a name")
    program_id = slugify(name)
    if repo.get_program(program_id) is not None:
        raise HTTPException(409, f"a service with the id {program_id} already exists")
    program = repo.put_program(
        ServiceProgram(
            program_id=program_id, name=name, category=body.category,
            description=body.description, aliases=body.aliases, source="manual",
            created_by=principal.user_id, created_at=utcnow(),
        )
    )
    invalidate()
    return {"program": program.model_dump(mode="json")}


@router.patch("/services/{program_id}")
def update_service(
    program_id: str,
    body: UpdateProgramIn,
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    program = repo.get_program(program_id)
    if program is None:
        raise HTTPException(404, "service not found")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(program, field, value)
    program.updated_at = utcnow()
    repo.put_program(program)
    invalidate()
    return {"program": program.model_dump(mode="json")}


@router.post("/services/{program_id}/items", status_code=201)
def add_item(
    program_id: str,
    body: CreateItemIn,
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    if repo.get_program(program_id) is None:
        raise HTTPException(404, "service not found")
    item = repo.put_catalog_item(
        ServiceItem(
            program_id=program_id, item_id=slugify(body.name), name=body.name.strip(),
            aliases=body.aliases, default_frequency=body.default_frequency,
            source="manual", created_at=utcnow(),
        )
    )
    invalidate()
    return {"item": item.model_dump(mode="json")}


@router.post("/services/{program_id}/aliases")
def add_alias(
    program_id: str,
    body: AliasIn,
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    """Teach the catalog another name for a service it already knows."""
    program = repo.get_program(program_id)
    if program is None:
        raise HTTPException(404, "service not found")
    alias = body.alias.strip()
    if alias and alias not in program.aliases and alias != program.name:
        program.aliases = sorted({*program.aliases, alias})
        program.updated_at = utcnow()
        repo.put_program(program)
    repo.delete_unmatched(normalise(alias))
    invalidate()
    return {"program": program.model_dump(mode="json")}


@router.post("/services/unmatched/{normalised}:link")
def link_unmatched(
    normalised: str,
    body: LinkIn,
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    """Map an unknown name onto an existing service, as an alias."""
    program = repo.get_program(body.program_id)
    if program is None:
        raise HTTPException(404, "service not found")
    entry = next((u for u in repo.list_unmatched() if u.normalised == normalised), None)
    if entry is None:
        raise HTTPException(404, "that name is not in the queue")
    program.aliases = sorted({*program.aliases, entry.raw})
    program.updated_at = utcnow()
    repo.put_program(program)
    repo.delete_unmatched(normalised)
    invalidate()
    return {"program": program.model_dump(mode="json"), "linked": entry.raw}


@router.post("/services/unmatched/{normalised}:promote", status_code=201)
def promote_unmatched(
    normalised: str,
    body: PromoteIn,
    principal: Principal = Depends(require_provider_principal),
    repo: Repository = Depends(get_repo),
):
    """Accept an unknown name as a service in its own right."""
    entry = next((u for u in repo.list_unmatched() if u.normalised == normalised), None)
    if entry is None:
        raise HTTPException(404, "that name is not in the queue")
    name = (body.name or entry.raw).strip()
    program = repo.put_program(
        ServiceProgram(
            program_id=slugify(name), name=name, category=body.category,
            aliases=[entry.raw] if entry.raw != name else [],
            source="manual", created_by=principal.user_id, created_at=utcnow(),
        )
    )
    repo.delete_unmatched(normalised)
    invalidate()
    return {"program": program.model_dump(mode="json")}


@router.get("/engagements/{engagement_id}/coverage")
def engagement_coverage(
    engagement_id: str,
    member: Membership = Depends(membership_dep),
    repo: Repository = Depends(get_repo),
):
    """Which of Wheels' services this engagement's agreements show it buying.

    A program counts when a pricing term resolves to it, priced or not: a contract that names a
    programme and charges nothing for it is still a service the customer receives.
    """
    snapshot = load_snapshot(repo)
    coverage = repo.list_coverage(engagement_id)

    availed: dict[str, dict[str, Any]] = {}
    for row in coverage:
        entry = availed.setdefault(
            row.program_id,
            {"program_id": row.program_id, "items": set(), "priced": 0, "documents": set()},
        )
        entry["items"].update(row.item_ids)
        entry["priced"] += row.priced_item_count
        entry["documents"].add(row.document_id)

    rows = []
    for program in snapshot.active_programs:
        got = availed.get(program.program_id)
        rows.append(
            {
                "program_id": program.program_id,
                "name": program.name,
                "category": program.category,
                "availed": got is not None,
                "items_availed": len(got["items"]) if got else 0,
                "items_total": len(snapshot.items_for(program.program_id)),
                "priced_items": got["priced"] if got else 0,
                "documents": sorted(got["documents"]) if got else [],
            }
        )
    rows.sort(key=lambda r: (not r["availed"], r["name"]))

    return {
        "programs": {
            "availed": sum(1 for r in rows if r["availed"]),
            "total": len(rows),
        },
        "items": {
            "availed": sum(r["items_availed"] for r in rows),
            "total": sum(r["items_total"] for r in rows),
        },
        "rows": rows,
    }
