"""Domain models: User, Requisition (PR header), RequisitionItem, ApprovalStep."""
from __future__ import annotations

from decimal import Decimal

from django.contrib.auth.models import AbstractUser
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models


class User(AbstractUser):
    """Custom user; `role` drives API permissions."""

    class Role(models.TextChoices):
        REQUESTOR = "REQUESTOR", "Requestor"
        SENIOR_APPROVER = "SENIOR_APPROVER", "Senior approver"
        ADMIN = "ADMIN", "Admin"
        PURCHASING_AGENT = "PURCHASING_AGENT", "Purchasing agent"

    role = models.CharField(max_length=20, choices=Role.choices, default=Role.REQUESTOR)
    department = models.CharField(max_length=100, blank=True)


class Requisition(models.Model):
    """Purchase Requisition header (maps to one SAP PR / PO document)."""

    class Status(models.TextChoices):
        DRAFT = "DRAFT", "Draft"
        PENDING = "PENDING", "Pending approval"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"
        EXPORTED = "EXPORTED", "Exported to SAP sheet"

    pr_number = models.CharField(max_length=20, unique=True)  # generated server-side
    header_note = models.TextField(blank=True)  # requestor's brief of the requirement
    requestor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="requisitions"
    )
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.DRAFT)
    total_estimated_cost = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0.00")
    )
    # SAP organisational data (EKORG / BUKRS); filled by the purchasing admin if needed.
    # Purchasing group lives on each item.
    purchasing_org = models.CharField(max_length=4, blank=True)
    company_code = models.CharField(max_length=4, blank=True)
    # Extras needed by the export sheet (BSART / LIFNR)
    document_type = models.CharField(max_length=4, default="NB")
    vendor_number = models.CharField(max_length=10, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return self.pr_number


class RequisitionItem(models.Model):
    """PR line item: a MATERIAL or a SERVICE (each uses its own subset of fields)."""

    class ItemType(models.TextChoices):
        MATERIAL = "MATERIAL", "Material"
        SERVICE = "SERVICE", "Service"

    class MaterialGroup(models.TextChoices):
        DRONES_DOMESTIC = "05147", "05147 (B/O drones - Domestic)"
        DRONES_IMPORT = "13147", "13147 (B/O drones - Import)"

    requisition = models.ForeignKey(Requisition, on_delete=models.CASCADE, related_name="items")
    line_number = models.PositiveIntegerField()
    item_type = models.CharField(max_length=10, choices=ItemType.choices)

    # Common to both types
    short_text = models.CharField(max_length=40)
    quantity = models.DecimalField(max_digits=13, decimal_places=3)
    unit_of_measure = models.CharField(max_length=3)

    # Material-only
    material_number = models.CharField(max_length=18, blank=True)  # S/X code from the digitizer
    delivery_date = models.DateField(null=True, blank=True)
    purchasing_group = models.CharField(max_length=3, default="77")
    total_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    plant = models.CharField(max_length=4, default="7206")
    storage_location = models.CharField(max_length=4, default="GS27", blank=True)
    requisitioner = models.CharField(max_length=12, blank=True)  # indentor ID, e.g. E52114
    material_group = models.CharField(max_length=5, choices=MaterialGroup.choices, blank=True)

    # Service-only
    gross_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="INR")
    cost_center = models.CharField(max_length=10, blank=True)

    @property
    def value(self) -> Decimal:
        """Estimated value: budgetary total for materials, qty x gross price for services."""
        if self.item_type == self.ItemType.MATERIAL:
            return self.total_value or Decimal("0")
        return self.quantity * (self.gross_price or Decimal("0"))

    class Meta:
        ordering = ["line_number"]
        constraints = [
            models.UniqueConstraint(fields=["requisition", "line_number"], name="uniq_pr_line"),
            models.CheckConstraint(check=models.Q(quantity__gt=0), name="item_qty_positive"),
        ]

    def save(self, *args, **kwargs) -> None:
        """Line items are immutable once the parent requisition leaves DRAFT."""
        if self.requisition.status != Requisition.Status.DRAFT:
            raise ValidationError(
                f"Items cannot be modified: requisition {self.requisition.pr_number} "
                f"is {self.requisition.status}, not DRAFT."
            )
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        if self.requisition.status != Requisition.Status.DRAFT:
            raise ValidationError("Items cannot be deleted unless the requisition is DRAFT.")
        return super().delete(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.requisition_id}/{self.line_number}"


class Plant(models.Model):
    """SAP plant master (WERKS) - reference data."""

    code = models.CharField(max_length=4, unique=True)
    name = models.CharField(max_length=40)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} - {self.name}"


class CostCenter(models.Model):
    """SAP cost center master (KOSTL) - reference data."""

    code = models.CharField(max_length=10, unique=True)
    name = models.CharField(max_length=40)

    class Meta:
        ordering = ["code"]

    def __str__(self) -> str:
        return f"{self.code} - {self.name}"


class ApprovalStep(models.Model):
    """One approver's decision in the sequential approval chain."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    requisition = models.ForeignKey(Requisition, on_delete=models.CASCADE, related_name="steps")
    approver = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="approval_steps"
    )
    step_order = models.PositiveIntegerField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    comments = models.TextField(blank=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["requisition", "step_order"]
        constraints = [
            models.UniqueConstraint(fields=["requisition", "step_order"], name="uniq_pr_step"),
            models.UniqueConstraint(fields=["requisition", "approver"], name="uniq_pr_approver"),
        ]
