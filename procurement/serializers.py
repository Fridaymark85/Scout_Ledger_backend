"""DRF serializers for requisitions, items and approval steps."""
from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from django.contrib.auth.password_validation import validate_password
from django.db import connection, models, transaction
from django.utils import timezone
from rest_framework import serializers

from .models import ApprovalStep, CostCenter, Plant, Requisition, RequisitionItem, User

FORMULA_PREFIXES = "=+-@"


def sanitize_text(value: str) -> str:
    """Strip leading whitespace and formula-trigger characters (CSV/Excel injection)."""
    return value.lstrip(FORMULA_PREFIXES + " \t\r\n").strip()


class SafeCharField(serializers.CharField):
    def to_internal_value(self, data: Any) -> str:
        return sanitize_text(super().to_internal_value(data))


class SanitizedSerializerMixin:
    """Maps every model CharField/TextField to SafeCharField."""

    serializer_field_mapping = {
        **serializers.ModelSerializer.serializer_field_mapping,
        models.CharField: SafeCharField,
        models.TextField: SafeCharField,
    }


class RequisitionItemSerializer(SanitizedSerializerMixin, serializers.ModelSerializer):
    class Meta:
        model = RequisitionItem
        fields = [
            "id", "line_number", "item_type", "short_text", "quantity", "unit_of_measure",
            "material_number", "delivery_date", "purchasing_group", "total_value", "plant",
            "storage_location", "requisitioner", "material_group",
            "gross_price", "currency", "cost_center",
        ]
        # S/X code comes from the digitizer, never from the requestor.
        read_only_fields = ["material_number"]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        """Each item type must supply its own mandatory fields."""
        if attrs["item_type"] == RequisitionItem.ItemType.MATERIAL:
            required = ["delivery_date", "total_value", "requisitioner", "material_group"]
        else:
            required = ["gross_price", "cost_center"]
        missing = {f: "This field is required." for f in required if not attrs.get(f)}
        if missing:
            raise serializers.ValidationError(missing)
        return attrs


class ApprovalStepSerializer(SanitizedSerializerMixin, serializers.ModelSerializer):
    """Read view of a step; only `status` and `comments` are writable (by the approver)."""

    approver_name = serializers.CharField(source="approver.get_full_name", read_only=True)

    class Meta:
        model = ApprovalStep
        fields = [
            "id", "requisition", "approver", "approver_name", "step_order",
            "status", "comments", "decided_at",
        ]
        read_only_fields = ["requisition", "approver", "step_order", "decided_at"]

    def validate_status(self, value: str) -> str:
        # An approver can only decide; resetting to PENDING is not allowed.
        if value == ApprovalStep.Status.PENDING:
            raise serializers.ValidationError("Decision must be APPROVED or REJECTED.")
        return value


class RequisitionSerializer(SanitizedSerializerMixin, serializers.ModelSerializer):
    items = RequisitionItemSerializer(many=True)
    steps = ApprovalStepSerializer(many=True, read_only=True)
    requestor_name = serializers.SerializerMethodField()

    def get_requestor_name(self, obj: Requisition) -> str:
        return obj.requestor.get_full_name() or obj.requestor.username

    def validate_vendor_number(self, value: str) -> str:
        """SAP LIFNR is 10 chars: zero-pad shorter codes, reject longer/non-alphanumeric ones."""
        if not value:
            return value
        if not value.isalnum():
            raise serializers.ValidationError("Vendor code must be alphanumeric.")
        if len(value) > 10:
            raise serializers.ValidationError("Vendor code cannot exceed 10 characters.")
        return value.zfill(10)

    class Meta:
        model = Requisition
        fields = [
            "id", "pr_number", "header_note", "requestor", "requestor_name", "status", "total_estimated_cost",
            "purchasing_org", "company_code",
            "document_type", "vendor_number", "created_at", "items", "steps",
        ]
        # Status, total and PR number are server-controlled; requestor comes from the session.
        read_only_fields = ["pr_number", "requestor", "status", "total_estimated_cost", "created_at"]

    @staticmethod
    def _total(items: list[dict[str, Any]]) -> Decimal:
        total = Decimal("0.00")
        for i in items:
            if i["item_type"] == RequisitionItem.ItemType.MATERIAL:
                total += i["total_value"]
            else:
                total += i["quantity"] * i["gross_price"]
        # Header total is DECIMAL(12,2); reject instead of failing at the database.
        if total >= Decimal("10000000000"):
            raise serializers.ValidationError({"items": "Estimated total exceeds the supported maximum."})
        return total.quantize(Decimal("0.01"))

    def validate_items(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not items:
            raise serializers.ValidationError("At least one item is required.")
        lines = [i["line_number"] for i in items]
        if len(lines) != len(set(lines)):
            raise serializers.ValidationError("Line numbers must be unique.")
        return items

    @transaction.atomic
    def create(self, validated_data: dict[str, Any]) -> Requisition:
        items = validated_data.pop("items")
        req = Requisition.objects.create(
            pr_number=f"PR{timezone.now():%y%m%d}-{uuid.uuid4().hex[:6].upper()}",
            requestor=self.context["request"].user,
            total_estimated_cost=self._total(items),
            **validated_data,
        )
        RequisitionItem.objects.bulk_create(RequisitionItem(requisition=req, **i) for i in items)
        return req

    @transaction.atomic
    def update(self, instance: Requisition, validated_data: dict[str, Any]) -> Requisition:
        # bulk_create/queryset.delete bypass RequisitionItem.save(), so enforce immutability here too.
        if instance.status != Requisition.Status.DRAFT:
            raise serializers.ValidationError("Only DRAFT requisitions can be modified.")
        items = validated_data.pop("items", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if items is not None:  # full replacement of line items
            instance.items.all().delete()
            RequisitionItem.objects.bulk_create(
                RequisitionItem(requisition=instance, **i) for i in items
            )
            instance.total_estimated_cost = self._total(items)
        instance.save()
        return instance


class PlantSerializer(serializers.ModelSerializer):
    class Meta:
        model = Plant
        fields = ["id", "code", "name"]


class CostCenterSerializer(serializers.ModelSerializer):
    class Meta:
        model = CostCenter
        fields = ["id", "code", "name"]


class UserAdminSerializer(serializers.ModelSerializer):
    """Admin view of users; admins can change role/department/active flag."""

    class Meta:
        model = User
        fields = ["id", "username", "first_name", "last_name", "email", "role", "department", "is_active"]
        read_only_fields = ["username"]


class UserRegistrationSerializer(serializers.ModelSerializer):
    """Public sign-up. The first user ever becomes an active ADMIN; everyone after is a locked REQUESTOR."""

    password = serializers.CharField(write_only=True, style={"input_type": "password"})
    email = serializers.EmailField()

    class Meta:
        model = User
        fields = ["id", "username", "email", "password", "role", "is_active"]
        read_only_fields = ["id", "role", "is_active"]  # never accepted from the client

    def validate_email(self, value: str) -> str:
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("An account with this email already exists.")
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        # Run Django's password policy with the user's own details (similarity check).
        validate_password(attrs["password"], User(username=attrs["username"], email=attrs["email"]))
        return attrs

    @transaction.atomic
    def create(self, validated_data: dict[str, Any]) -> User:
        password = validated_data.pop("password")
        # Serialise concurrent sign-ups so two "first" users cannot both become ADMIN.
        if connection.vendor == "postgresql":
            with connection.cursor() as cur:
                cur.execute("SELECT pg_advisory_xact_lock(%s)", [727001])
        admin_exists = User.objects.filter(role=User.Role.ADMIN).exists()
        user = User(
            **validated_data,
            role=User.Role.REQUESTOR if admin_exists else User.Role.ADMIN,
            is_active=not admin_exists,
        )
        user.set_password(password)
        user.save()
        return user


class UpdateCredentialsSerializer(serializers.Serializer):
    """Logged-in user changes their own username and/or password (admin hand-over)."""

    current_password = serializers.CharField(write_only=True, style={"input_type": "password"})
    username = serializers.CharField(required=False, max_length=150)
    password = serializers.CharField(required=False, write_only=True, style={"input_type": "password"})

    def validate_current_password(self, value: str) -> str:
        # Re-authenticate so a stolen session alone cannot take over the account.
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("Current password is incorrect.")
        return value

    def validate_username(self, value: str) -> str:
        user = self.context["request"].user
        if User.objects.filter(username__iexact=value).exclude(pk=user.pk).exists():
            raise serializers.ValidationError("This username is already taken.")
        User._meta.get_field("username").run_validators(value)  # Django's allowed-character rules
        return value

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if "username" not in attrs and "password" not in attrs:
            raise serializers.ValidationError("Provide a new username and/or a new password.")
        if "password" in attrs:
            user = self.context["request"].user
            validate_password(attrs["password"], user)
        return attrs

    def save(self, **kwargs: Any) -> User:
        user = self.context["request"].user
        if "username" in self.validated_data:
            user.username = self.validated_data["username"]
        if "password" in self.validated_data:
            user.set_password(self.validated_data["password"])
        user.save()
        return user


class ReassignSerializer(serializers.Serializer):
    """Admin hands a pending approval step to another senior approver."""

    approver_id = serializers.IntegerField()

    def validate_approver_id(self, value: int) -> int:
        if not User.objects.filter(id=value, role=User.Role.SENIOR_APPROVER, is_active=True).exists():
            raise serializers.ValidationError("Must be an active SENIOR_APPROVER.")
        return value


class SubmitSerializer(serializers.Serializer):
    """Ordered list of approver ids that forms the approval chain on submission."""

    approver_ids = serializers.ListField(child=serializers.IntegerField(), allow_empty=False)

    def validate_approver_ids(self, ids: list[int]) -> list[int]:
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("Duplicate approvers.")
        found = User.objects.filter(
            id__in=ids, role=User.Role.SENIOR_APPROVER, is_active=True
        ).count()
        if found != len(ids):
            raise serializers.ValidationError("All approvers must be active SENIOR_APPROVER users.")
        return ids
