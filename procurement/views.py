"""ViewSets: requisitions (CRUD + submit + export) and approval steps (decide)."""
from __future__ import annotations

from django.db import transaction
from django.db.models import QuerySet
from django.http import HttpResponse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.decorators.cache import cache_page
from django.views.decorators.vary import vary_on_headers
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle

from .excel_generator import generate_requisition_workbook
from .models import ApprovalStep, CostCenter, Plant, Requisition, User
from .permissions import ApprovalStepPermission, RequisitionPermission, UserAdminPermission
from .serializers import (
    ApprovalStepSerializer, CostCenterSerializer, PlantSerializer, ReassignSerializer,
    RequisitionSerializer, SubmitSerializer, UpdateCredentialsSerializer, UserAdminSerializer,
    UserRegistrationSerializer,
)

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


# cache_page wraps dispatch, i.e. runs before DRF authentication. Varying on
# Authorization keys the cache per credential so anonymous callers can never be
# served a response cached for an authenticated user (401s are never cached).
@method_decorator(vary_on_headers("Authorization"), name="list")
@method_decorator(cache_page(60 * 60 * 24), name="list")
class PlantViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only plant master data, cached in Redis for 24h."""

    queryset = Plant.objects.all()
    serializer_class = PlantSerializer


@method_decorator(vary_on_headers("Authorization"), name="list")
@method_decorator(cache_page(60 * 60 * 24), name="list")
class CostCenterViewSet(viewsets.ReadOnlyModelViewSet):
    """Read-only cost center master data, cached in Redis for 24h."""

    queryset = CostCenter.objects.all()
    serializer_class = CostCenterSerializer


class RequisitionViewSet(viewsets.ModelViewSet):
    serializer_class = RequisitionSerializer
    permission_classes = [RequisitionPermission]

    def get_queryset(self) -> QuerySet[Requisition]:
        """Row-level visibility: requestors see own, approvers their assigned, others all."""
        qs = Requisition.objects.select_related("requestor").prefetch_related("items", "steps__approver")
        user = self.request.user
        if user.role == User.Role.REQUESTOR:
            return qs.filter(requestor=user)
        if user.role == User.Role.SENIOR_APPROVER:
            return qs.filter(steps__approver=user).distinct()
        return qs

    def _assert_editable(self, req: Requisition) -> None:
        if req.status != Requisition.Status.DRAFT:
            raise ValidationError("Only DRAFT requisitions can be modified.")

    def perform_update(self, serializer: RequisitionSerializer) -> None:
        self._assert_editable(serializer.instance)
        serializer.save()

    def perform_destroy(self, instance: Requisition) -> None:
        self._assert_editable(instance)
        instance.delete()

    @action(detail=True, methods=["post"])
    @transaction.atomic
    def submit(self, request: Request, pk: str | None = None) -> Response:
        """DRAFT -> PENDING; creates the ordered approval chain."""
        req = self.get_object()
        self._assert_editable(req)
        ser = SubmitSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        ApprovalStep.objects.bulk_create(
            ApprovalStep(requisition=req, approver_id=uid, step_order=n)
            for n, uid in enumerate(ser.validated_data["approver_ids"], start=1)
        )
        req.status = Requisition.Status.PENDING
        req.save(update_fields=["status", "updated_at"])
        # Re-fetch so the prefetched `steps` cache does not hide the new rows.
        return Response(self.get_serializer(self.get_queryset().get(pk=req.pk)).data)

    @action(detail=True, methods=["get"])
    @transaction.atomic
    def export(self, request: Request, pk: str | None = None) -> HttpResponse:
        """Download the SAP sheet for an APPROVED requisition and mark it EXPORTED."""
        req = self.get_object()
        try:
            buffer = generate_requisition_workbook(req)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        req.status = Requisition.Status.EXPORTED
        req.save(update_fields=["status", "updated_at"])
        response = HttpResponse(buffer.getvalue(), content_type=XLSX_MIME)
        response["Content-Disposition"] = f'attachment; filename="{req.pr_number}.xlsx"'
        return response


class ApprovalStepViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """Steps are created by `submit`; approvers only record their decision here."""

    serializer_class = ApprovalStepSerializer
    permission_classes = [ApprovalStepPermission]

    def get_queryset(self) -> QuerySet[ApprovalStep]:
        qs = ApprovalStep.objects.select_related("approver", "requisition")
        user = self.request.user
        if user.role == User.Role.SENIOR_APPROVER:
            return qs.filter(approver=user)
        if user.role == User.Role.REQUESTOR:
            return qs.filter(requisition__requestor=user)
        return qs

    @transaction.atomic
    def perform_update(self, serializer: ApprovalStepSerializer) -> None:
        # Lock the requisition so concurrent decisions cannot race.
        step = serializer.instance
        req = Requisition.objects.select_for_update().get(pk=step.requisition_id)
        if req.status != Requisition.Status.PENDING or step.status != ApprovalStep.Status.PENDING:
            raise ValidationError("This step is no longer actionable.")
        # Sequential chain for approvers; the admin may override the order.
        is_admin = self.request.user.role == User.Role.ADMIN
        if not is_admin and req.steps.filter(step_order__lt=step.step_order).exclude(
            status=ApprovalStep.Status.APPROVED
        ).exists():
            raise PermissionDenied("Earlier approval steps are still outstanding.")

        comments = serializer.validated_data.get("comments", "")
        if is_admin:  # keep an audit trail of overrides
            comments = f"[Admin override by {self.request.user.username}] {comments}".strip()
        decision = serializer.save(decided_at=timezone.now(), comments=comments)

        if decision.status == ApprovalStep.Status.REJECTED:
            req.status = Requisition.Status.REJECTED
        elif not req.steps.filter(status=ApprovalStep.Status.PENDING).exists():
            req.status = Requisition.Status.APPROVED
        req.save(update_fields=["status", "updated_at"])

    @action(detail=True, methods=["post"])
    def reassign(self, request: Request, pk: str | None = None) -> Response:
        """ADMIN: hand a still-pending step to a different senior approver."""
        step = self.get_object()
        ser = ReassignSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        new_id = ser.validated_data["approver_id"]
        if step.status != ApprovalStep.Status.PENDING:
            raise ValidationError("Only pending steps can be reassigned.")
        if step.requisition.steps.filter(approver_id=new_id).exists():
            raise ValidationError("That approver is already in this chain.")
        step.approver_id = new_id
        step.save(update_fields=["approver"])
        return Response(self.get_serializer(ApprovalStep.objects.get(pk=step.pk)).data)


class RegisterThrottle(SimpleRateThrottle):
    """Per-IP limit for public sign-up (rate from THROTTLE_REGISTER)."""

    scope = "register"

    def get_cache_key(self, request, view):
        return self.cache_format % {"scope": self.scope, "ident": self.get_ident(request)}


class UserViewSet(
    mixins.ListModelMixin,
    mixins.RetrieveModelMixin,
    mixins.UpdateModelMixin,
    viewsets.GenericViewSet,
):
    """ADMIN: list users and change role / department / active flag."""

    queryset = User.objects.order_by("username")
    serializer_class = UserAdminSerializer
    permission_classes = [UserAdminPermission]

    @action(detail=False, methods=["patch"], url_path="me/credentials", permission_classes=[IsAuthenticated])
    def credentials(self, request: Request) -> Response:
        """Change own username/password. The client must log in again afterwards."""
        ser = UpdateCredentialsSerializer(data=request.data, context={"request": request})
        ser.is_valid(raise_exception=True)
        user = ser.save()
        return Response({"username": user.username, "detail": "Credentials updated. Please log in again."})

    @action(
        detail=False, methods=["post"], permission_classes=[AllowAny], authentication_classes=[],
        throttle_classes=[RegisterThrottle],
    )
    def register(self, request: Request) -> Response:
        """Public sign-up: first account becomes ADMIN, later ones are inactive REQUESTORs."""
        ser = UserRegistrationSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = ser.save()
        return Response(
            {"username": user.username, "role": user.role, "is_active": user.is_active},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=False, methods=["get"], permission_classes=[IsAuthenticated])
    def me(self, request: Request) -> Response:
        """Profile of the signed-in user (any role); the frontend calls this after login."""
        return Response(UserAdminSerializer(request.user).data)

    @action(detail=False, methods=["get"])
    def approvers(self, request: Request) -> Response:
        """Active senior approvers (id + name only) for the submit-chain picker."""
        qs = User.objects.filter(role=User.Role.SENIOR_APPROVER, is_active=True)
        return Response([{"id": u.id, "name": u.get_full_name() or u.username} for u in qs])
