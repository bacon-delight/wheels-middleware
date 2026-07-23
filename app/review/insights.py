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

from ..llm.base import Tool
from ..store.models import Submission
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
    "finance_request_changes": "Finance requested changes",
    "finance_approve": "Finance approved",
    "setup_billing": "Billing set up",
    "billing_done": "Billing active",
}
# Comments only the provider side should see in the summary/thread.
_INTERNAL_ACTIONS = {"finance_request_changes"}


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


def _service_terms(fields) -> dict[str, str]:
    out: dict[str, str] = {}
    for f in fields:
        if not f.elected:
            continue
        lines = [fee_line(fi) for fi in f.fee_items]
        out[f.service] = " ; ".join(x for x in lines if x) or "(no fee terms)"
    return out


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

_CHANGE_SYSTEM = (
    "You verify that a provider's re-uploaded fleet-billing agreement reflects the changes a "
    "client requested. You are given the client's requested changes and a diff of what actually "
    "changed between the previous and revised terms. For each changed or requested service, judge "
    "whether the request was: 'applied' (fully), 'partial' (moved in the right direction but not "
    "all the way — e.g. a smaller discount than asked; acceptable but must be flagged), "
    "'not_applied' (unchanged despite being asked), 'unrelated' (a change the client did not ask "
    "for), or 'new'. Be concise and factual; use dollar/percentage specifics."
)


def _changes_and_versions(repo: Repository, sub: Submission) -> tuple[list[dict], str]:
    eid = sub.engagement_id
    changes: list[dict[str, Any]] = []
    sig_parts: list[str] = []
    for did in (sub.msa_document_id, sub.mla_document_id):
        if not did:
            continue
        doc = repo.get_document(eid, did)
        if not doc:
            continue
        cur = doc.current_version
        sig_parts.append(f"{did}:{cur}")
        if cur < 2:
            continue
        new_terms = _service_terms(repo.list_fields(eid, did, cur))
        old_terms = _service_terms(repo.list_fields(eid, did, cur - 1))
        for svc in sorted(set(new_terms) | set(old_terms)):
            before = old_terms.get(svc, "(not present)")
            after = new_terms.get(svc, "(not present)")
            if before != after:
                changes.append(
                    {"service": svc, "doc": doc.doc_type, "before": before, "after": after}
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
    applicable = any(":" in p and int(p.split(":")[1]) >= 2 for p in version_sig.split("|") if p)
    req_hash = hashlib.md5(requested.encode()).hexdigest()[:8]  # noqa: S324 - cache key, not security
    cache_key = f"{version_sig}::{req_hash}"
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
