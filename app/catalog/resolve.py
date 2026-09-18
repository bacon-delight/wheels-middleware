"""Match a contract's own wording onto the catalog.

Contracts do not agree with each other about what a service is called. Across the corpus the
same family appears as "Fuel Management Program", "Fuel Management", "Fleet Card Program (Fuel)"
and "FUEL MANAGEMENT PROGRAM (SECTION 2(B) OF THIS SOW)". Unless those collapse onto one entry,
"19 of 35 programs availed" counts nothing.

So resolution is a ladder, tried in order and reported honestly:

    exact -> alias -> normalised -> fuzzy -> unmatched

The last rung is the important one. An unmatched name is **not an error and never a silent
drop**: the record keeps the contract's wording, is stored and shown, and joins a queue for a
person to map or promote. Guessing would corrupt the denominator; discarding would lose a term
the customer is paying for.

This module is pure. It takes a snapshot and a string, and touches no storage.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from .models import CatalogSnapshot, ServiceItem, ServiceProgram

MATCH_KINDS = ("exact", "alias", "normalised", "fuzzy", "unmatched")

# Words that carry no distinguishing information in a service name. Stripping them is what makes
# "Fuel Management" and "Fuel Management Program" the same thing.
_STOPWORDS = frozenset(
    {
        "program",
        "programs",
        "service",
        "services",
        "the",
        "of",
        "this",
        "sow",
        "section",
        "agreement",
        "and",
        "a",
        "an",
    }
)

_PARENS = re.compile(r"\([^)]*\)")
_PUNCT = re.compile(r"[^a-z0-9 ]+")
_SPACE = re.compile(r"\s+")

# Accept a fuzzy match at this ratio, but only when it is clearly better than the runner-up.
# Without the margin, two similarly-named programs would take turns winning depending on how
# the contract happened to word it, which is worse than admitting we do not know.
_FUZZY_FLOOR = 0.86
_FUZZY_MARGIN = 0.08


def normalise(name: str | None) -> str:
    """The comparison key: lowercase, no punctuation, no parentheticals, no filler words."""
    if not name:
        return ""
    text = _PARENS.sub(" ", str(name).lower())
    text = _PUNCT.sub(" ", text)
    words = [w for w in _SPACE.sub(" ", text).split() if w and w not in _STOPWORDS]
    # A crude singular, enough to join "Renewals" to "Renewal" without a stemmer in the Lambda.
    words = [
        w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w
        for w in words
    ]
    return " ".join(words)


def slugify(name: str) -> str:
    """A stable, readable id. Deterministic so the seed file diffs cleanly between runs."""
    text = _PARENS.sub(" ", (name or "").lower())
    text = _PUNCT.sub(" ", text)
    words = [w for w in _SPACE.sub(" ", text).split() if w and w not in _STOPWORDS]
    return "-".join(words)[:60] or "unnamed"


@dataclass(frozen=True)
class ResolvedService:
    program: ServiceProgram | None = None
    item: ServiceItem | None = None
    kind: str = "unmatched"

    @property
    def program_id(self) -> str | None:
        return self.program.program_id if self.program else None

    @property
    def item_id(self) -> str | None:
        return self.item.item_id if self.item else None

    @property
    def confident(self) -> bool:
        """Whether this needs a human's eye before it counts toward coverage."""
        return self.kind in ("exact", "alias", "normalised")


def _fuzzy(key: str, candidates: dict[str, object]) -> object | None:
    if not key or not candidates:
        return None
    scored = sorted(
        (
            (difflib.SequenceMatcher(None, key, candidate_key).ratio(), value)
            for candidate_key, value in candidates.items()
        ),
        key=lambda pair: pair[0],
        reverse=True,
    )
    best_score, best = scored[0]
    if best_score < _FUZZY_FLOOR:
        return None
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_score - runner_up < _FUZZY_MARGIN:
        return None  # two plausible answers is the same as none
    return best


def resolve_program(snapshot: CatalogSnapshot, raw: str | None) -> ResolvedService:
    if not raw or not raw.strip():
        return ResolvedService()
    name = raw.strip()
    programs = snapshot.active_programs

    for program in programs:
        if program.name.strip() == name:
            return ResolvedService(program=program, kind="exact")

    for program in programs:
        if any(a.strip() == name for a in program.aliases):
            return ResolvedService(program=program, kind="alias")

    key = normalise(name)
    by_key: dict[str, ServiceProgram] = {}
    for program in programs:
        by_key.setdefault(normalise(program.name), program)
        for alias in program.aliases:
            by_key.setdefault(normalise(alias), program)

    if key and key in by_key:
        return ResolvedService(program=by_key[key], kind="normalised")

    hit = _fuzzy(key, by_key)
    if hit is not None:
        return ResolvedService(program=hit, kind="fuzzy")
    return ResolvedService()


def resolve_item(
    snapshot: CatalogSnapshot, program: ServiceProgram | None, raw: str | None
) -> tuple[ServiceItem | None, str]:
    """Resolve an item **within** a program. Never across programs."""
    if program is None or not raw or not raw.strip():
        return None, "unmatched"
    name = raw.strip()
    items = [i for i in snapshot.items_for(program.program_id) if i.status == "ACTIVE"]

    for item in items:
        if item.name.strip() == name:
            return item, "exact"
    for item in items:
        if any(a.strip() == name for a in item.aliases):
            return item, "alias"

    key = normalise(name)
    by_key: dict[str, ServiceItem] = {}
    for item in items:
        by_key.setdefault(normalise(item.name), item)
        for alias in item.aliases:
            by_key.setdefault(normalise(alias), item)

    if key and key in by_key:
        return by_key[key], "normalised"
    hit = _fuzzy(key, by_key)
    if hit is not None:
        return hit, "fuzzy"
    return None, "unmatched"


def resolve(snapshot: CatalogSnapshot, program_raw: str | None, item_raw: str | None = None):
    """Resolve a pricing record's program, and its item within that program."""
    found = resolve_program(snapshot, program_raw)
    if found.program is None:
        return found
    item, item_kind = resolve_item(snapshot, found.program, item_raw)
    # The program match is what the record is filed under, so it sets the reported kind; a
    # weaker item match must not make a confident program look doubtful.
    kind = found.kind if item is None else max(found.kind, item_kind, key=MATCH_KINDS.index)
    return ResolvedService(program=found.program, item=item, kind=kind)
