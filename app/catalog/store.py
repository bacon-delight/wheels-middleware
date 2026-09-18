"""Loading the catalog, and keeping it warm.

The whole catalog is one small partition — forty-odd programs and a few hundred items — so it
loads in a single query. It is cached per process because the extract worker resolves every
pricing record against it and a Lambda handles many records per invocation.
"""

from __future__ import annotations

import json
import pathlib
import time

from ..store.repository import Repository, utcnow
from .models import CatalogSnapshot, ServiceItem, ServiceProgram

SEED_PATH = pathlib.Path(__file__).parent / "seed_catalog.json"

_CACHE: tuple[float, CatalogSnapshot] | None = None
_TTL_SECONDS = 300


def load_snapshot(repo: Repository, *, fresh: bool = False) -> CatalogSnapshot:
    """The catalog as one object. Cached briefly; `fresh` after an edit."""
    global _CACHE
    now = time.monotonic()
    if not fresh and _CACHE is not None and now - _CACHE[0] < _TTL_SECONDS:
        return _CACHE[1]
    programs, items = repo.load_catalog()
    snapshot = CatalogSnapshot(programs=programs, items=items)
    _CACHE = (now, snapshot)
    return snapshot


def invalidate() -> None:
    global _CACHE
    _CACHE = None


def seed_from_file(repo: Repository, path: pathlib.Path | None = None) -> tuple[int, int]:
    """Load the reviewed seed file into the table. Idempotent: the ids are deterministic."""
    data = json.loads((path or SEED_PATH).read_text())
    now = utcnow()
    programs = items = 0
    for entry in data.get("programs", []):
        repo.put_program(
            ServiceProgram(
                program_id=entry["program_id"],
                name=entry["name"],
                aliases=entry.get("aliases", []),
                category=entry.get("category"),
                status=entry.get("status", "ACTIVE"),
                source=entry.get("source", "derived"),
                contract_count=entry.get("contract_count", 0),
                created_at=now,
            )
        )
        programs += 1
        for item in entry.get("items", []):
            repo.put_catalog_item(
                ServiceItem(
                    program_id=entry["program_id"],
                    item_id=item["item_id"],
                    name=item["name"],
                    aliases=item.get("aliases", []),
                    created_at=now,
                )
            )
            items += 1
    invalidate()
    return programs, items
