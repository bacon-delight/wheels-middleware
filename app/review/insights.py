"""LLM-assisted review insights, both with deterministic fallbacks so a model outage never
breaks the page:

- change verification: after a re-upload, does the revised agreement reflect what the client
  asked for? Flags applied / partial / not-applied / unrelated per service.
- negotiation summary: an executive summary + a client-safe thread of the back-and-forth.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from ..catalog.resolve import normalise
from ..llm.base import Tool
from ..store.models import DocumentStanding, Submission
from ..store.repository import Repository

log = logging.getLogger(__name__)

# Audit actions that represent communication / decisions in the negotiation.
COMM_ACTIONS = {
    "engagement_created": "Engagement created",
    "submit_to_client": "Terms submitted to client",
    "client_request_changes": "Client requested changes",
    "resubmit_to_client": "Provider responded & resubmitted",
    "reupload": "Provider re-uploaded revised terms",
    "client_approve": "Client approved terms",
    "billing_corrected": "Billing audit corrected a charge",
    "billing_ready": "Billing generated",
    "approve_billing": "Billing audit approved — engagement live",
}
# Comments only the provider side should see in the summary/thread.
_INTERNAL_ACTIONS = {"billing_corrected"}


# --- fee formatting (mirrors the UI's feeLine so prompts read like the app) ---
def fee_line(fi: dict[str, Any]) -> str:
    parts: list[str] = []
    if fi.get("amount") is not None:
        parts.append(f"${fi['amount']}")
    if fi.get("rate_pct") is not None:
        prefix = "cost + " if fi.get("fee_type") == "cost_plus" else ""
        parts.append(f"{prefix}{fi['rate_pct']}%")
    if fi.get("unit_basis"):
        parts.append(str(fi["unit_basis"]).replace("_", " "))
    if fi.get("minimum") is not None:
        parts.append(f"min ${fi['minimum']}")
    for t in fi.get("tier_bands") or []:
        parts.append(f"units {t.get('min_units')}-{t.get('max_units', '∞')}: ${t.get('amount')}")
    return " · ".join(parts)


def _priced_terms(terms) -> dict[str, tuple[str, str]]:
    """What each program costs: comparison key -> (name to show, the priced line).

    Pricing only. A re-upload diff exists to answer whether the money changed, and threading a
    definition that gained a comma through it would bury the answer.

    Both sides of the comparison are readings, not the document itself, so wording that differs
    between two reads of the same page must not read as a change. Identity therefore comes from
    the catalog where a term resolved and from the normalised label otherwise, the unit basis
    comes from the normalised field rather than the contract's phrasing, and a row carrying no
    money at all is left out — it cannot be evidence that the money moved.
    """
    out: dict[str, dict[str, Any]] = {}
    for t in terms:
        if t.category != "pricing" or t.superseded:
            continue
        record = t.record or {}
        program = record.get("program") or t.subtitle or t.title
        # Money, not merely a row. A unit basis on its own ("per occurrence") is how the term
        # would be charged if it were charged — it cannot show that a price moved, and two
        # reads of one page disagree about such rows often enough to bury the real finding.
        priced = (
            record.get("amount") is not None
            or record.get("rate_pct") is not None
            or record.get("minimum") is not None
            or bool(record.get("tier_bands"))
        )
        if not priced:
            continue
        line = fee_line(
            {
                "amount": record.get("amount"),
                "rate_pct": record.get("rate_pct"),
                "fee_type": record.get("fee_type"),
                "unit_basis": t.unit_basis or record.get("frequency"),
                "minimum": record.get("minimum"),
                "tier_bands": record.get("tier_bands"),
            }
        )
        key = t.program_id or normalise(program) or str(program).lower()
        label = t.catalog_item_id or normalise(record.get("item")) or "program"
        entry = out.setdefault(key, {"display": program, "lines": []})
        entry["lines"].append(f"{label}: {line}")
    return {
        key: (v["display"], " ; ".join(sorted(v["lines"])))
        for key, v in out.items()
    }


# ============================ change verification ============================
_CHANGE_TOOL = Tool(
    name="report_change_review",
    description="Report whether the revised terms reflect the client's requested changes.",
    input_schema={
        "type": "object",
        "properties": {
            "overall": {"type": "string", "description": "1-2 sentence verdict."},
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "service": {"type": "string"},
                        "requested": {"type": "string", "description": "What the client asked."},
                        "delivered": {"type": "string", "description": "What actually changed."},
                        "status": {
                            "type": "string",
                            "enum": ["applied", "partial", "not_applied", "unrelated", "new"],
                        },
                        "note": {"type": "string"},
                    },
                    "required": ["service", "status", "note"],
                },
            },
        },
        "required": ["overall", "items"],
    },
)

# Bumped whenever the comparison changes shape, so stored assessments are not reused across it.
_COMPARISON_VERSION = 3

_CHANGE_SYSTEM = (
    "You verify that a provider's re-uploaded fleet-billing agreement reflects the changes a "
    "client requested. You are given the client's requested changes and a diff of what actually "
    "changed between the previous and revised terms. For each changed or requested service, judge "
    "whether the request was: 'applied' (fully), 'partial' (moved in the right direction but not "
    "all the way — e.g. a smaller discount than asked; acceptable but must be flagged), "
    "'not_applied' (unchanged despite being asked), 'unrelated' (a change the client did not ask "
    "for), or 'new'. Be concise and factual; use dollar/percentage specifics."
)


def _predecessor(repo: Repository, eid: str, doc) -> tuple[str, int] | None:
    """The reading this agreement should be compared against, or None on a first upload.

    A revision arrives one of two ways, and the answer has to survive both. Replacing an
    agreement bumps it to a new version, so the previous reading is version-1 of the same
    document. Uploading the revision as its own file instead supersedes the old document, and
    the previous reading is the newest superseded agreement of the same type. Only versions
    within one document were handled before, so a re-upload that arrived as a new file looked
    like a first upload and there was nothing to verify.
    """
    if doc.current_version >= 2:
        return doc.document_id, doc.current_version - 1
    older = [
        d
        for d in repo.list_documents(eid)
        if d.document_id != doc.document_id
        and d.doc_type == doc.doc_type
        and d.standing == DocumentStanding.SUPERSEDED.value
        and repo.term_counts(eid, d.document_id, d.current_version)
    ]
    if not older:
        return None
    previous = max(older, key=lambda d: d.created_at)
    return previous.document_id, previous.current_version


def _changes_and_versions(repo: Repository, sub: Submission) -> tuple[list[dict], str]:
    eid = sub.engagement_id
    changes: list[dict[str, Any]] = []
    sig_parts: list[str] = []
    for did in sub.docs().values():
        doc = repo.get_document(eid, did)
        if not doc:
            continue
        cur = doc.current_version
        previous = _predecessor(repo, eid, doc)
        # The signature names both sides, so the cached assessment is dropped when either the
        # revision or the thing it is compared against changes.
        sig_parts.append(
            f"{did}:{cur}" + (f"<{previous[0]}:{previous[1]}" if previous else "")
        )
        if previous is None:
            continue
        new_terms = _priced_terms(repo.list_terms(eid, did, cur, "pricing"))
        old_terms = _priced_terms(repo.list_terms(eid, previous[0], previous[1], "pricing"))
        for key in sorted(set(new_terms) | set(old_terms)):
            was, before = old_terms.get(key, ("", "(not present)"))
            now, after = new_terms.get(key, ("", "(not present)"))
            if before != after:
                changes.append(
                    {
                        "service": now or was, "doc": doc.doc_type,
                        "before": before, "after": after,
                    }
                )
    return changes, "|".join(sig_parts)


def _latest_client_request(repo: Repository, eid: str) -> str:
    reqs = [
        e.comment
        for e in repo.list_audit(eid)
        if e.action == "client_request_changes" and e.comment
    ]
    return reqs[-1] if reqs else ""


def _assess_changes(requested: str, changes: list[dict]) -> dict[str, Any]:
    try:
        from ..llm.factory import get_provider

        table = "\n".join(
            f"- {c['service']} ({c['doc']}): BEFORE [{c['before']}]  AFTER [{c['after']}]"
            for c in changes
        ) or "(no field-level differences detected between versions)"
        user = (
            f"CLIENT REQUESTED CHANGES:\n{requested or '(none recorded)'}\n\n"
            f"WHAT CHANGED IN THE REVISED TERMS:\n{table}"
        )
        result = get_provider().call_tool(
            system=_CHANGE_SYSTEM, user_text=user, tool=_CHANGE_TOOL, max_tokens=1500
        )
        return result.data
    except Exception as e:  # noqa: BLE001 - degrade to a deterministic diff
        log.warning("change-review LLM failed: %s", e)
        return {
            "overall": "Automated diff shown (AI assessment unavailable) — verify against the "
            "client's request manually.",
            "items": [
                {
                    "service": c["service"],
                    "requested": requested or "—",
                    "delivered": f"{c['before']} → {c['after']}",
                    "status": "new",
                    "note": "Value changed between versions.",
                }
                for c in changes
            ],
        }


def build_change_review(repo: Repository, sub: Submission) -> dict[str, Any]:
    """Assess the current (re-uploaded) terms against the client's request; cached on the sub."""
    changes, version_sig = _changes_and_versions(repo, sub)
    requested = _latest_client_request(repo, sub.engagement_id)
    # Verifiable when some agreement in force has an earlier reading to be held against.
    applicable = any("<" in p for p in version_sig.split("|") if p)
    req_hash = hashlib.md5(requested.encode()).hexdigest()[:8]  # noqa: S324 - cache key, not security
    cache_key = f"v{_COMPARISON_VERSION}::{version_sig}::{req_hash}"
    if not applicable:
        return {"applicable": False}
    cached = sub.change_review
    if cached and cached.get("cache_key") == cache_key:
        return cached
    assessment = _assess_changes(requested, changes)
    out = {
        "applicable": True,
        "cache_key": cache_key,
        "requested": requested,
        "changes": changes,
        "overall": assessment.get("overall", ""),
        "items": assessment.get("items", []),
    }
    sub.change_review = out
    repo.put_submission(sub)
    return out


# ============================ negotiation summary ============================
_SUMMARY_TOOL = Tool(
    name="report_negotiation_summary",
    description="Summarize the negotiation history for a business audience.",
    input_schema={
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "3-5 sentence executive summary."},
            "highlights": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["summary", "highlights"],
    },
)
_SUMMARY_SYSTEM = (
    "You summarize the negotiation history of a fleet-billing contract for a business audience. "
    "Given chronological events (who did what, with any comments), write a short, neutral "
    "executive summary of the back-and-forth and list the key highlights as short bullet "
    "strings. No fluff; be specific about requests and outcomes."
)


def _thread(events, for_client: bool) -> list[dict[str, Any]]:
    rows = []
    for e in sorted(events, key=lambda x: x.ts):
        if e.action not in COMM_ACTIONS:
            continue
        if for_client and e.action in _INTERNAL_ACTIONS:
            continue
        rows.append(
            {
                "ts": e.ts,
                "actor_name": e.actor_name,
                "actor_role": e.actor_role,
                "action": e.action,
                "label": COMM_ACTIONS[e.action],
                "comment": None if (for_client and e.action in _INTERNAL_ACTIONS) else e.comment,
            }
        )
    return rows


def _summarize(rows: list[dict]) -> dict[str, Any]:
    if not rows:
        return {"summary": "Nothing has happened on this engagement yet.", "highlights": []}
    try:
        from ..llm.factory import get_provider

        transcript = "\n".join(
            f"{r['ts'][:10]} — {r['actor_name'] or r['actor_role']}: {r['label']}"
            + (f" — “{r['comment']}”" if r["comment"] else "")
            for r in rows
        )
        result = get_provider().call_tool(
            system=_SUMMARY_SYSTEM, user_text=transcript, tool=_SUMMARY_TOOL, max_tokens=1200
        )
        return result.data
    except Exception as e:  # noqa: BLE001 - degrade to a plain recap
        log.warning("summary LLM failed: %s", e)
        last = rows[-1]
        return {
            "summary": f"{len(rows)} steps recorded. Most recent: {last['label']}"
            + (f" — “{last['comment']}”" if last["comment"] else "") + ".",
            "highlights": [],
        }


def build_summary(repo: Repository, sub: Submission, for_client: bool) -> dict[str, Any]:
    events = repo.list_audit(sub.engagement_id)
    comm = [e for e in events if e.action in COMM_ACTIONS]
    count = len(comm)
    cached = sub.nego_summary
    if cached and cached.get("count") == count:
        narrative = cached
    else:
        # Summarize from client-safe rows so the same narrative is safe to show either party.
        narrative = {"count": count, **_summarize(_thread(comm, for_client=True))}
        sub.nego_summary = narrative
        repo.put_submission(sub)
    return {
        "summary": narrative["summary"],
        "highlights": narrative.get("highlights", []),
        "thread": _thread(comm, for_client=for_client),
    }
