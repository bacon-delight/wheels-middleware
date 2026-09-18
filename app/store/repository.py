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
from ..lifecycle.submission_state import Role
from . import keys as k
from .models import (
    AuditEvent,
    Customer,
    Document,
    DocumentVersion,
    Engagement,
    Membership,
    Payment,
    ProviderUser,
    ReviewField,
    Submission,
    UserProfile,
    Vehicle,
    VehicleAssignment,
    VehicleStatus,
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
    """One handle per table: the main single-table store plus the vehicle inventory.

    Vehicles live apart because inventory listing is an org-wide access pattern with a much
    larger row count than the engagement workflow; keeping it here would have meant a hot
    synthetic partition or a scan.
    """

    def __init__(
        self,
        table_name: str | None = None,
        resource=None,
        vehicles_table_name: str | None = None,
    ):
        settings = get_settings()
        self.table_name = table_name or settings.table_name
        self.vehicles_table_name = vehicles_table_name or settings.vehicles_table_name
        if resource is None:
            import boto3

            resource = boto3.resource("dynamodb", region_name=settings.core_region)
        self.table = resource.Table(self.table_name)
        self.vehicles = resource.Table(self.vehicles_table_name)

    def _put_vehicle_item(self, item: dict[str, Any]) -> None:
        self.vehicles.put_item(Item=_to_decimal(item))

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
        for meta in ("PK", "SK", "type", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK"):
            data.pop(meta, None)
        return model_cls.model_validate(data)

    def _scan_by_type(self, type_: str) -> list[dict[str, Any]]:
        """Paginated scan filtered to one item type (org-level, low-volume aggregation)."""
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "FilterExpression": "#t = :t",
            "ExpressionAttributeNames": {"#t": "type"},
            "ExpressionAttributeValues": {":t": type_},
        }
        while True:
            r = self.table.scan(**kwargs)
            items.extend(r.get("Items", []))
            lek = r.get("LastEvaluatedKey")
            if not lek:
                return items
            kwargs["ExclusiveStartKey"] = lek

    # --- Engagement ---
    def put_engagement(self, e: Engagement) -> Engagement:
        gsi: dict[str, str] = {
            "GSI1PK": k.lcstatus_gsi1pk(e.status),
            "GSI1SK": k.eng_pk(e.engagement_id),
        }
        if e.customer_id:
            # GSI2 answers "this customer's engagements" without a scan.
            gsi["GSI2PK"] = k.customer_gsi2pk(e.customer_id)
            gsi["GSI2SK"] = k.eng_pk(e.engagement_id)
        self._put(
            self._model_item(
                e, k.eng_pk(e.engagement_id), k.engagement_meta_sk(), "ENGAGEMENT", **gsi
            )
        )
        return e

    def get_engagement(self, engagement_id: str) -> Engagement | None:
        r = self.table.get_item(
            Key={"PK": k.eng_pk(engagement_id), "SK": k.engagement_meta_sk()}
        )
        return self._load(r.get("Item"), Engagement)

    def list_all_engagements(self) -> list[Engagement]:
        """Every engagement in the org (finance/provider org-level view). Small-scale scan."""
        items = self._scan_by_type("ENGAGEMENT")
        return [self._load(i, Engagement) for i in items]

    def list_all_submissions(self) -> list[Submission]:
        """Every submission across the org (finance dashboard status join). Small-scale scan."""
        return [self._load(i, Submission) for i in self._scan_by_type("SUBMISSION")]

    def set_engagement_status(self, engagement_id: str, status: str) -> None:
        """Denormalize the submission lifecycle status onto the engagement meta.

        The GSI1 status partition is rewritten alongside it; leaving it behind would make the
        index disagree with the row it points at.
        """
        self.table.update_item(
            Key={"PK": k.eng_pk(engagement_id), "SK": k.engagement_meta_sk()},
            UpdateExpression="SET #s = :s, #g = :g",
            ExpressionAttributeNames={"#s": "status", "#g": "GSI1PK"},
            ExpressionAttributeValues={":s": status, ":g": k.lcstatus_gsi1pk(status)},
        )

    def set_engagement_billing(
        self, engagement_id: str, fleet_size: int, monthly_recurring: float | None
    ) -> None:
        self.table.update_item(
            Key={"PK": k.eng_pk(engagement_id), "SK": k.engagement_meta_sk()},
            UpdateExpression="SET fleet_size = :f, monthly_recurring = :m",
            ExpressionAttributeValues=_to_decimal(
                {":f": fleet_size, ":m": monthly_recurring}
            ),
        )

    def set_engagement_contract(
        self,
        engagement_id: str,
        start: str | None,
        term_months: int | None,
        end: str | None,
        auto_renew: bool,
        renewal_notice_days: int,
    ) -> None:
        """Set the contract term. `end` is stored rather than derived so a negotiated end date
        that does not fall exactly `term_months` after the start is representable."""
        self.table.update_item(
            Key={"PK": k.eng_pk(engagement_id), "SK": k.engagement_meta_sk()},
            UpdateExpression=(
                "SET contract_start = :s, contract_term_months = :t, contract_end = :e, "
                "auto_renew = :a, renewal_notice_days = :n"
            ),
            ExpressionAttributeValues=_to_decimal({
                ":s": start, ":t": term_months, ":e": end,
                ":a": auto_renew, ":n": renewal_notice_days,
            }),
        )

    def set_engagement_schedule(
        self, engagement_id: str, billing_start: str, billing_frequency: str
    ) -> None:
        self.table.update_item(
            Key={"PK": k.eng_pk(engagement_id), "SK": k.engagement_meta_sk()},
            UpdateExpression="SET billing_start = :s, billing_frequency = :f",
            ExpressionAttributeValues={":s": billing_start, ":f": billing_frequency},
        )

    # --- Customers ---
    def put_customer(self, c: Customer) -> Customer:
        self._put(
            self._model_item(c, k.org_customers_pk(), k.customer_sk(c.customer_id), "CUSTOMER")
        )
        return c

    def get_customer(self, customer_id: str) -> Customer | None:
        r = self.table.get_item(
            Key={"PK": k.org_customers_pk(), "SK": k.customer_sk(customer_id)}
        )
        return self._load(r.get("Item"), Customer)

    def list_customers(self) -> list[Customer]:
        """One query on a small dedicated partition — no scan."""
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {
            "KeyConditionExpression": Key("PK").eq(k.org_customers_pk())
            & Key("SK").begins_with("CUST#")
        }
        while True:
            r = self.table.query(**kwargs)
            items.extend(r.get("Items", []))
            lek = r.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        return [self._load(i, Customer) for i in items]

    def update_customer(self, customer_id: str, patch: dict[str, Any]) -> Customer | None:
        if not patch:
            return self.get_customer(customer_id)
        names, values, sets = {}, {":now": utcnow()}, ["updated_at = :now"]
        for i, (key, val) in enumerate(patch.items()):
            names[f"#p{i}"] = key
            values[f":p{i}"] = val
            sets.append(f"#p{i} = :p{i}")
        r = self.table.update_item(
            Key={"PK": k.org_customers_pk(), "SK": k.customer_sk(customer_id)},
            UpdateExpression="SET " + ", ".join(sets),
            ConditionExpression="attribute_exists(PK)",
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=_to_decimal(values),
            ReturnValues="ALL_NEW",
        )
        return self._load(r["Attributes"], Customer)

    def list_customer_engagements(self, customer_id: str) -> list[Engagement]:
        """GSI2: customer -> engagements."""
        r = self.table.query(
            IndexName="GSI2",
            KeyConditionExpression=Key("GSI2PK").eq(k.customer_gsi2pk(customer_id)),
        )
        return [self._load(i, Engagement) for i in r.get("Items", [])]

    def set_engagement_customer(
        self, engagement_id: str, customer_id: str, client_name: str
    ) -> None:
        """Link an engagement to a customer and write the GSI2 keys that index it."""
        self.table.update_item(
            Key={"PK": k.eng_pk(engagement_id), "SK": k.engagement_meta_sk()},
            UpdateExpression=(
                "SET customer_id = :c, client_name = :n, GSI2PK = :g2p, GSI2SK = :g2s"
            ),
            ExpressionAttributeValues={
                ":c": customer_id,
                ":n": client_name,
                ":g2p": k.customer_gsi2pk(customer_id),
                ":g2s": k.eng_pk(engagement_id),
            },
        )

    def set_engagement_scope(self, engagement_id: str, scope: str | None) -> None:
        self.table.update_item(
            Key={"PK": k.eng_pk(engagement_id), "SK": k.engagement_meta_sk()},
            UpdateExpression="SET #sc = :s",
            ExpressionAttributeNames={"#sc": "scope"},
            ExpressionAttributeValues={":s": scope},
        )

    def set_engagement_name(self, engagement_id: str, name: str) -> None:
        self.table.update_item(
            Key={"PK": k.eng_pk(engagement_id), "SK": k.engagement_meta_sk()},
            UpdateExpression="SET #n = :n",
            ExpressionAttributeNames={"#n": "name"},
            ExpressionAttributeValues={":n": name},
        )

    def set_fleet_override(self, engagement_id: str, override: int | None) -> None:
        self.table.update_item(
            Key={"PK": k.eng_pk(engagement_id), "SK": k.engagement_meta_sk()},
            UpdateExpression="SET fleet_size_override = :o",
            ExpressionAttributeValues=_to_decimal({":o": override}),
        )

    # --- Payments (billing schedule) ---
    def put_payment(self, p: Payment) -> Payment:
        self._put(
            self._model_item(p, k.eng_pk(p.engagement_id), k.payment_sk(p.seq), "PAYMENT")
        )
        return p

    def get_payment(self, engagement_id: str, seq: int) -> Payment | None:
        r = self.table.get_item(
            Key={"PK": k.eng_pk(engagement_id), "SK": k.payment_sk(seq)}
        )
        return self._load(r.get("Item"), Payment)

    def list_payments(self, engagement_id: str) -> list[Payment]:
        r = self.table.query(
            KeyConditionExpression=Key("PK").eq(k.eng_pk(engagement_id))
            & Key("SK").begins_with("PAY#")
        )
        return [self._load(i, Payment) for i in r.get("Items", [])]

    def list_all_payments(self) -> list[Payment]:
        return [self._load(i, Payment) for i in self._scan_by_type("PAYMENT")]

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

    def list_user_memberships(self, user_id: str) -> list[Membership]:
        r = self.table.query(
            IndexName="GSI1", KeyConditionExpression=Key("GSI1PK").eq(k.user_gsi1pk(user_id))
        )
        return [
            self._load(i, Membership) for i in r.get("Items", []) if i.get("type") == "MEMBERSHIP"
        ]

    # --- User profile ---
    def get_user_profile(self, user_id: str) -> UserProfile | None:
        r = self.table.get_item(Key={"PK": k.user_pk(user_id), "SK": k.profile_sk()})
        return self._load(r.get("Item"), UserProfile)

    def put_user_profile(self, p: UserProfile) -> UserProfile:
        self._put(self._model_item(p, k.user_pk(p.user_id), k.profile_sk(), "USER_PROFILE"))
        return p

    # --- Org-level provider directory ---
    def put_provider_user(self, pu: ProviderUser) -> ProviderUser:
        self._put(
            self._model_item(
                pu, k.org_providers_pk(), k.membership_sk(pu.user_id), "PROVIDER_DIR"
            )
        )
        return pu

    def get_provider_user(self, user_id: str) -> ProviderUser | None:
        r = self.table.get_item(
            Key={"PK": k.org_providers_pk(), "SK": k.membership_sk(user_id)}
        )
        return self._load(r.get("Item"), ProviderUser)

    def list_provider_directory(self) -> list[ProviderUser]:
        r = self.table.query(
            KeyConditionExpression=Key("PK").eq(k.org_providers_pk())
            & Key("SK").begins_with("USER#")
        )
        return [self._load(i, ProviderUser) for i in r.get("Items", [])]

    def list_all_provider_memberships(self) -> list[Membership]:
        """Provider/finance memberships across all engagements (to backfill the directory)."""
        members = [self._load(i, Membership) for i in self._scan_by_type("MEMBERSHIP")]
        return [m for m in members if m.role in (Role.PROVIDER, Role.FINANCE)]

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
        # The engagement keeps a denormalized copy of this status so lists and the dashboard
        # need no join. Updating it here rather than at each call site is what stops the two
        # drifting apart: the pipeline workers transition submissions too, and they did not.
        self.set_engagement_status(engagement_id, new_status)
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

    def set_document_meta(
        self,
        engagement_id: str,
        document_id: str,
        *,
        doc_type: str | None = None,
        standing: str | None = None,
        effective_date: str | None = None,
        classified_type: str | None = None,
        confidence: float | None = None,
        type_overridden: bool | None = None,
    ) -> None:
        """Patch the classification fields the parse worker (or a correcting analyst) sets."""
        updates = {
            "doc_type": doc_type, "standing": standing, "effective_date": effective_date,
            "classified_type": classified_type, "classification_confidence": confidence,
            "type_overridden": type_overridden,
        }
        updates = {k: v for k, v in updates.items() if v is not None}
        if not updates:
            return
        names, values, sets = {}, {}, []
        for i, (key, val) in enumerate(updates.items()):
            names[f"#f{i}"] = key
            values[f":f{i}"] = val
            sets.append(f"#f{i} = :f{i}")
        self.table.update_item(
            Key={"PK": k.eng_pk(engagement_id), "SK": k.document_sk(document_id)},
            UpdateExpression="SET " + ", ".join(sets),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=_to_decimal(values),
        )

    def delete_document(self, engagement_id: str, document_id: str) -> int:
        """Remove a document and everything derived from it.

        The document, its versions and its extracted review fields all share the `DOC#{id}`
        sort-key prefix, so one query finds the lot. Leaving the fields behind would keep a
        deleted agreement's terms in the billing config.
        """
        items, kwargs = [], {
            "KeyConditionExpression": Key("PK").eq(k.eng_pk(engagement_id))
            & Key("SK").begins_with(k.document_sk(document_id)),
            "ProjectionExpression": "PK, SK",
        }
        while True:
            r = self.table.query(**kwargs)
            items.extend(r.get("Items", []))
            lek = r.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        with self.table.batch_writer() as batch:
            for it in items:
                batch.delete_item(Key={"PK": it["PK"], "SK": it["SK"]})
        return len(items)

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

    # --- Vehicles (separate inventory table) ---
    def _vehicle_item(self, v: Vehicle) -> dict[str, Any]:
        item = v.model_dump(mode="json")
        item.update(
            {
                "PK": k.vehicle_pk(v.vehicle_id),
                "SK": k.vehicle_meta_sk(),
                "type": "VEHICLE",
                "GSI1PK": k.fleet_gsi1pk(v.ownership),
                "GSI1SK": k.vehicle_gsi1sk(v.status, v.duty_band, v.vehicle_id),
            }
        )
        # GSI2 is sparse: written only while the vehicle is assigned.
        if v.engagement_id:
            item["GSI2PK"] = k.vehicle_gsi2pk(v.engagement_id)
            item["GSI2SK"] = k.vehicle_gsi2sk(v.vehicle_id)
        return item

    def put_vehicle(self, v: Vehicle) -> Vehicle:
        self._put_vehicle_item(self._vehicle_item(v))
        return v

    def get_vehicle(self, vehicle_id: str) -> Vehicle | None:
        r = self.vehicles.get_item(
            Key={"PK": k.vehicle_pk(vehicle_id), "SK": k.vehicle_meta_sk()}
        )
        return self._load(r.get("Item"), Vehicle)

    def list_vehicles_by_ownership(
        self, ownership: str, status: str | None = None, duty_band: str | None = None
    ) -> list[Vehicle]:
        """GSI1: one partition per ownership class, sliced by the status/duty sort key."""
        cond = Key("GSI1PK").eq(k.fleet_gsi1pk(ownership))
        if status and duty_band:
            cond = cond & Key("GSI1SK").begins_with(f"ST#{status}#DUTY#{duty_band}#")
        elif status:
            cond = cond & Key("GSI1SK").begins_with(f"ST#{status}#")
        return self._query_vehicles("GSI1", cond, duty_band if not status else None)

    def list_vehicles_by_engagement(self, engagement_id: str) -> list[Vehicle]:
        """GSI2: vehicles currently assigned to one engagement."""
        return self._query_vehicles(
            "GSI2", Key("GSI2PK").eq(k.vehicle_gsi2pk(engagement_id)), None
        )

    def _query_vehicles(self, index: str, cond, duty_band: str | None) -> list[Vehicle]:
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"IndexName": index, "KeyConditionExpression": cond}
        while True:
            r = self.vehicles.query(**kwargs)
            items.extend(r.get("Items", []))
            lek = r.get("LastEvaluatedKey")
            if not lek:
                break
            kwargs["ExclusiveStartKey"] = lek
        out = [self._load(i, Vehicle) for i in items]
        if duty_band:
            out = [v for v in out if v.duty_band == duty_band]
        return out

    def vehicle_status_count(self, ownership: str, status: str) -> int:
        """Inventory tile counts. Select=COUNT transfers no items."""
        total, kwargs = 0, {
            "IndexName": "GSI1",
            "KeyConditionExpression": Key("GSI1PK").eq(k.fleet_gsi1pk(ownership))
            & Key("GSI1SK").begins_with(f"ST#{status}#"),
            "Select": "COUNT",
        }
        while True:
            r = self.vehicles.query(**kwargs)
            total += r.get("Count", 0)
            lek = r.get("LastEvaluatedKey")
            if not lek:
                return total
            kwargs["ExclusiveStartKey"] = lek

    def count_vehicles_for_engagement(self, engagement_id: str) -> int:
        """Drives the derived fleet size. Uses Select=COUNT so nothing is transferred."""
        total, kwargs = 0, {
            "IndexName": "GSI2",
            "KeyConditionExpression": Key("GSI2PK").eq(k.vehicle_gsi2pk(engagement_id)),
            "Select": "COUNT",
        }
        while True:
            r = self.vehicles.query(**kwargs)
            total += r.get("Count", 0)
            lek = r.get("LastEvaluatedKey")
            if not lek:
                return total
            kwargs["ExclusiveStartKey"] = lek

    def update_vehicle(self, vehicle_id: str, patch: dict[str, Any]) -> Vehicle | None:
        """Re-put the whole item so the three index key pairs stay consistent with it."""
        current = self.get_vehicle(vehicle_id)
        if current is None:
            return None
        merged = current.model_dump(mode="json") | patch
        merged["updated_at"] = utcnow()
        updated = Vehicle.model_validate(merged)
        self._put_vehicle_item(self._vehicle_item(updated))
        return updated

    def assign_vehicle(
        self, vehicle_id: str, engagement_id: str, customer_id: str | None, actor_id: str
    ) -> Vehicle | None:
        now = utcnow()
        v = self.update_vehicle(
            vehicle_id,
            {
                "engagement_id": engagement_id,
                "customer_id": customer_id,
                "assigned_at": now,
                "status": VehicleStatus.ASSIGNED.value,
            },
        )
        if v is None:
            return None
        self.put_vehicle_assignment(
            VehicleAssignment(
                vehicle_id=vehicle_id, assignment_id=new_id(), engagement_id=engagement_id,
                customer_id=customer_id, assigned_at=now, assigned_by=actor_id,
            )
        )
        return v

    def release_vehicle(self, vehicle_id: str) -> Vehicle | None:
        """Send a unit back to stock. Customer-owned units go to ON_ORDER, never IN_STOCK."""
        current = self.get_vehicle(vehicle_id)
        if current is None:
            return None
        from .models import VehicleOwnership

        back = (
            VehicleStatus.IN_STOCK.value
            if current.ownership == VehicleOwnership.WHEELS_OWNED.value
            else VehicleStatus.ON_ORDER.value
        )
        return self.update_vehicle(
            vehicle_id,
            {"engagement_id": None, "assigned_at": None, "status": back},
        )

    def put_vehicle_assignment(self, a: VehicleAssignment) -> VehicleAssignment:
        item = a.model_dump(mode="json")
        item.update(
            {
                "PK": k.vehicle_pk(a.vehicle_id),
                "SK": k.assignment_sk(a.assigned_at, a.assignment_id),
                "type": "VEHICLE_ASSIGNMENT",
            }
        )
        self._put_vehicle_item(item)
        return a

    def list_vehicle_assignments(self, vehicle_id: str) -> list[VehicleAssignment]:
        r = self.vehicles.query(
            KeyConditionExpression=Key("PK").eq(k.vehicle_pk(vehicle_id))
            & Key("SK").begins_with("ASG#"),
            ScanIndexForward=False,
        )
        return [self._load(i, VehicleAssignment) for i in r.get("Items", [])]
