"""Stored shapes for the service catalog."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ServiceItem(BaseModel):
    """One priced line a program can carry.

    Identity is the pair (program_id, item_id), never the label on its own: `Monthly Program
    Fee` appears under twenty-two different programs in the contract corpus, and collapsing
    those would make a nonsense of which services a customer buys.
    """

    program_id: str
    item_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    default_frequency: str | None = None
    status: str = "ACTIVE"
    source: str = "derived"  # derived | manual
    created_at: str | None = None


class ServiceProgram(BaseModel):
    """A named Wheels service family, e.g. the Fuel Management Program."""

    program_id: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    category: str | None = None
    description: str | None = None
    status: str = "ACTIVE"  # ACTIVE | DEPRECATED
    source: str = "derived"
    # How many contracts in the corpus named it. Display only, and a useful sanity check on
    # whether a program is real or an extraction artefact.
    contract_count: int = 0
    created_by: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class UnmatchedName(BaseModel):
    """A program name an extraction found that the catalog does not know.

    Never auto-promoted. A catalog that grows itself from every wording an extraction invents
    stops being a denominator worth measuring against, so these queue for a person who can say
    whether it is a new service or another name for one we have.
    """

    normalised: str
    raw: str
    count: int = 1
    examples: list[dict] = Field(default_factory=list)
    first_seen: str | None = None
    last_seen: str | None = None


class CatalogSnapshot(BaseModel):
    """The whole catalog, as one query returns it."""

    programs: list[ServiceProgram] = Field(default_factory=list)
    items: list[ServiceItem] = Field(default_factory=list)

    def items_for(self, program_id: str) -> list[ServiceItem]:
        return [i for i in self.items if i.program_id == program_id]

    @property
    def active_programs(self) -> list[ServiceProgram]:
        return [p for p in self.programs if p.status == "ACTIVE"]
