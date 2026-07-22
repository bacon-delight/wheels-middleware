"""DynamoDB single-table repository.

Floats are converted to Decimal on write and back to native numbers on read (DynamoDB does
not accept float). Submission status changes use an optimistic-concurrency conditional write
so a stale transition fails loudly instead of clobbering.
"""

from __future__ import annotations

import datetime
import decimal
import json
import uuid
from typing import Any

from boto3.dynamodb.conditions import Key

from ..config import get_settings
from . import keys as k
from .models import (
    AuditEvent,
    Document,
    DocumentVersion,
    Engagement,
    Membership,
    ReviewField,
    Submission,
)


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def utcnow() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat()


def _to_decimal(obj: Any) -> Any:
    return json.loads(json.dumps(obj), parse_float=decimal.Decimal)


def _to_native(obj: Any) -> Any:
    if isinstance(obj, list):
        return [_to_native(x) for x in obj]
    if isinstance(obj, dict):
        return {key: _to_native(v) for key, v in obj.items()}
    if isinstance(obj, decimal.Decimal):
        return int(obj) if obj == obj.to_integral_value() else float(obj)
    return obj


class ConflictError(Exception):
    """Raised when an optimistic-concurrency status transition loses a race."""


class Repository:
    def __init__(self, table_name: str | None = None, resource=None):
        self.table_name = table_name or get_settings().table_name
        if resource is None:
            import boto3

            resource = boto3.resource("dynamodb", region_name=get_settings().core_region)
        self.table = resource.Table(self.table_name)

    # --- serialization helpers ---
    def _put(self, item: dict[str, Any]) -> None:
        self.table.put_item(Item=_to_decimal(item))

    @staticmethod
    def _model_item(model, pk: str, sk: str, type_: str, **gsi: str) -> dict[str, Any]:
        item = model.model_dump(mode="json")
        item.update({"PK": pk, "SK": sk, "type": type_, **gsi})
        return item

    @staticmethod
    def _load(item: dict[str, Any] | None, model_cls):
        if not item:
            return None
        data = _to_native(item)
        for meta in ("PK", "SK", "type", "GSI1PK", "GSI1SK"):
            data.pop(meta, None)
        return model_cls.model_validate(data)

    # --- Engagement ---
    def put_engagement(self, e: Engagement) -> Engagement:
        self._put(
            self._model_item(
                e, k.eng_pk(e.engagement_id), k.engagement_meta_sk(), "ENGAGEMENT",
                GSI1PK=k.lcstatus_gsi1pk(e.status), GSI1SK=k.eng_pk(e.engagement_id),
            )
        )
        return e

    def get_engagement(self, engagement_id: str) -> Engagement | None:
        r = self.table.get_item(
            Key={"PK": k.eng_pk(engagement_id), "SK": k.engagement_meta_sk()}
        )
        return self._load(r.get("Item"), Engagement)

    # --- Membership ---
    def put_membership(self, m: Membership) -> Membership:
        self._put(
            self._model_item(
                m, k.eng_pk(m.engagement_id), k.membership_sk(m.user_id), "MEMBERSHIP",
                GSI1PK=k.user_gsi1pk(m.user_id), GSI1SK=k.eng_pk(m.engagement_id),
            )
        )
        return m

    def get_membership(self, engagement_id: str, user_id: str) -> Membership | None:
        r = self.table.get_item(
            Key={"PK": k.eng_pk(engagement_id), "SK": k.membership_sk(user_id)}
        )
        return self._load(r.get("Item"), Membership)

    def list_members(self, engagement_id: str) -> list[Membership]:
        r = self.table.query(
            KeyConditionExpression=Key("PK").eq(k.eng_pk(engagement_id))
            & Key("SK").begins_with("USER#")
        )
        return [self._load(i, Membership) for i in r.get("Items", [])]

    def list_user_engagement_ids(self, user_id: str) -> list[str]:
        r = self.table.query(
            IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq(k.user_gsi1pk(user_id))
        )
        return [i["GSI1SK"].split("#", 1)[1] for i in r.get("Items", [])]

    # --- Submission ---
    def put_submission(self, s: Submission) -> Submission:
        self._put(
            self._model_item(
                s, k.eng_pk(s.engagement_id), k.submission_sk(s.submission_id), "SUBMISSION",
                GSI1PK=k.lcstatus_gsi1pk(s.status.value), GSI1SK=k.submission_sk(s.submission_id),
            )
        )
        return s

    def get_submission(self, engagement_id: str, submission_id: str) -> Submission | None:
        r = self.table.get_item(
            Key={"PK": k.eng_pk(engagement_id), "SK": k.submission_sk(submission_id)}
        )
        return self._load(r.get("Item"), Submission)

    def list_submissions(self, engagement_id: str) -> list[Submission]:
        r = self.table.query(
            KeyConditionExpression=Key("PK").eq(k.eng_pk(engagement_id))
            & Key("SK").begins_with("SUB#"),
            FilterExpression="#t = :t",
            ExpressionAttributeNames={"#t": "type"},
            ExpressionAttributeValues={":t": "SUBMISSION"},
        )
        return [self._load(i, Submission) for i in r.get("Items", [])]

    def update_submission_status(
        self,
        engagement_id: str,
        submission_id: str,
        expected_status: str,
        new_status: str,
        extra: dict[str, Any] | None = None,
    ) -> Submission:
        names = {"#s": "status", "#g": "GSI1PK"}
        values = {
            ":new": new_status,
            ":expected": expected_status,
            ":now": utcnow(),
            ":g": k.lcstatus_gsi1pk(new_status),
        }
        set_parts = ["#s = :new", "updated_at = :now", "#g = :g"]
        for i, (key, val) in enumerate((extra or {}).items()):
            names[f"#e{i}"] = key
            values[f":e{i}"] = val
            set_parts.append(f"#e{i} = :e{i}")
        try:
            r = self.table.update_item(
                Key={"PK": k.eng_pk(engagement_id), "SK": k.submission_sk(submission_id)},
                UpdateExpression="SET " + ", ".join(set_parts),
                ConditionExpression="#s = :expected",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=_to_decimal(values),
                ReturnValues="ALL_NEW",
            )
        except self.table.meta.client.exceptions.ConditionalCheckFailedException as e:
            raise ConflictError(
                f"submission {submission_id} was not in expected status {expected_status}"
            ) from e
        return self._load(r["Attributes"], Submission)

    # --- Document + version ---
    def put_document(self, d: Document) -> Document:
        self._put(
            self._model_item(d, k.eng_pk(d.engagement_id), k.document_sk(d.document_id), "DOCUMENT")
        )
        return d

    def get_document(self, engagement_id: str, document_id: str) -> Document | None:
        r = self.table.get_item(
            Key={"PK": k.eng_pk(engagement_id), "SK": k.document_sk(document_id)}
        )
        return self._load(r.get("Item"), Document)

    def list_documents(self, engagement_id: str) -> list[Document]:
        r = self.table.query(
            KeyConditionExpression=Key("PK").eq(k.eng_pk(engagement_id))
            & Key("SK").begins_with("DOC#"),
            FilterExpression="#t = :t",
            ExpressionAttributeNames={"#t": "type"},
            ExpressionAttributeValues={":t": "DOCUMENT"},
        )
        return [self._load(i, Document) for i in r.get("Items", [])]

    def put_document_version(self, v: DocumentVersion) -> DocumentVersion:
        self._put(
            self._model_item(
                v, k.eng_pk(v.engagement_id), k.version_sk(v.document_id, v.version), "DOCVERSION"
            )
        )
        return v

    def get_document_version(
        self, engagement_id: str, document_id: str, version: int
    ) -> DocumentVersion | None:
        r = self.table.get_item(
            Key={"PK": k.eng_pk(engagement_id), "SK": k.version_sk(document_id, version)}
        )
        return self._load(r.get("Item"), DocumentVersion)

    # --- Review fields ---
    def put_field(self, f: ReviewField) -> ReviewField:
        self._put(
            self._model_item(
                f,
                k.eng_pk(f.engagement_id),
                k.field_sk(f.document_id, f.version, f.service, f.field_id),
                "FIELD",
                GSI1PK=k.doc_version_gsi1pk(f.document_id, f.version),
                GSI1SK=k.review_gsi1sk(f.needs_review, f.confidence),
            )
        )
        return f

    def list_fields(
        self, engagement_id: str, document_id: str, version: int
    ) -> list[ReviewField]:
        """Fields for a doc-version, needs-review first then ascending confidence (GSI1)."""
        r = self.table.query(
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq(k.doc_version_gsi1pk(document_id, version)),
            ScanIndexForward=True,
        )
        return [self._load(i, ReviewField) for i in r.get("Items", [])]

    def update_field(
        self, engagement_id: str, document_id: str, version: int, service: str, field_id: str,
        updates: dict[str, Any],
    ) -> None:
        names = {f"#u{i}": key for i, key in enumerate(updates)}
        values = {f":u{i}": val for i, val in enumerate(updates.values())}
        set_parts = [f"#u{i} = :u{i}" for i in range(len(updates))]
        self.table.update_item(
            Key={
                "PK": k.eng_pk(engagement_id),
                "SK": k.field_sk(document_id, version, service, field_id),
            },
            UpdateExpression="SET " + ", ".join(set_parts),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=_to_decimal(values),
        )

    # --- Audit ---
    def put_audit(self, a: AuditEvent) -> AuditEvent:
        self._put(
            self._model_item(
                a, k.eng_pk(a.engagement_id), k.audit_sk(a.ts, a.event_id), "AUDIT"
            )
        )
        return a

    def list_audit(self, engagement_id: str) -> list[AuditEvent]:
        r = self.table.query(
            KeyConditionExpression=Key("PK").eq(k.eng_pk(engagement_id))
            & Key("SK").begins_with("AUD#"),
            ScanIndexForward=False,
        )
        return [self._load(i, AuditEvent) for i in r.get("Items", [])]
